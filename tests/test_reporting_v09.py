from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from people_tracking_feishu.config import RuntimePaths
from people_tracking_feishu.reporting import (
    PUBLIC_DECISION_SCHEMA,
    apply_agent_review_decisions,
    build_agent_review_bundle,
    classify_delta_item,
    materialize_window_events,
)
from people_tracking_feishu.state import PortableState
from people_tracking_feishu.tracking import deliver_digest, prepare_digest


def runtime(tmp_path: Path) -> tuple[PortableState, RuntimePaths, dict]:
    paths = RuntimePaths(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        config_file=tmp_path / "config/config.json",
        database=tmp_path / "state/people.sqlite3",
        reports=tmp_path / "state/reports",
        install_state=tmp_path / "config/install-state.json",
    )
    paths.ensure()
    config = {
        "state": "enabled",
        "schedule": {"timezone": "Asia/Shanghai"},
        "outputs": {
            "public": {
                "document": {"enabled": False},
                "message": {
                    "enabled": True,
                    "target_kind": "chat",
                    "target_id": "public-only",
                },
            },
            "developer": {
                "document": {"enabled": False},
                "message": {
                    "enabled": True,
                    "target_kind": "chat",
                    "target_id": "developer-only",
                },
            },
        },
        "apis": {"deepseek": {"enabled": False}},
    }
    return PortableState(paths.database), paths, config


def add_person(state: PortableState, name: str = "Alice") -> tuple[str, str]:
    person = state.tracker.add_person(
        name, urls=[f"https://example.invalid/{name.casefold()}"]
    )
    return person["person_key"], person["sources"][0]["source_id"]


def add_observation(
    state: PortableState,
    *,
    source_id: str,
    observed_at: datetime,
    decision_status: str,
    delta: dict | None = None,
    summary: str = "synthetic",
    run_status: str = "completed",
    suffix: str = "1",
) -> str:
    run_id = f"run_{suffix}"
    observation_id = f"obs_{suffix}"
    at = observed_at.astimezone(timezone.utc).isoformat()
    state.db.execute(
        """INSERT INTO runs(run_id,started_at,completed_at,trigger,status,purpose)
           VALUES(?,?,?,?,?,'production')""",
        (run_id, at, at, "test", run_status),
    )
    state.db.execute(
        """INSERT INTO observations(
             observation_id,run_id,source_id,observed_at,health_status,semantic_hash,
             decision_status,score,delta_json,summary,reviewer
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            observation_id,
            run_id,
            source_id,
            at,
            "healthy" if decision_status == "changed" else "blocked",
            f"hash-{suffix}",
            decision_status,
            0.9,
            json.dumps(delta or {}, ensure_ascii=False),
            summary,
            "deterministic",
        ),
    )
    state.db.commit()
    return run_id


def paper_delta(title: str) -> dict:
    return {
        "additions": [
            {
                "category": "publication",
                "text": title,
                "stable_id": f"paper:{title}",
                "attributes": {"title": title},
            }
        ],
        "modifications": [],
        "removals": [],
    }


def approve_pending_papers(state: PortableState) -> None:
    bundle = build_agent_review_bundle(state, batch_size=200)
    decisions = []
    for request in bundle["requests"]:
        evidence = request["evidence"]
        raw = evidence.get("raw_item") or {}
        title = str(raw.get("text") or (raw.get("attributes") or {}).get("title") or "新论文")
        decisions.append(
            {
                "request_id": request["request_id"],
                "evidence_hash": request["evidence_hash"],
                "decision": "publish",
                "change_type": "publication_added",
                "headline": "新增论文",
                "what_changed": f"新增论文：{title}",
                "why_material": "这是一项具体的新论文成果",
                "confidence": 0.99,
                "evidence_ids": [evidence["event_id"]],
            }
        )
    if decisions:
        apply_agent_review_decisions(
            state,
            {
                "schema_version": PUBLIC_DECISION_SCHEMA,
                "review_snapshot_id": bundle["review_snapshot"]["snapshot_id"],
                "decided_by": "execution-agent:report-test",
                "decisions": decisions,
            },
        )


def test_public_and_developer_reports_are_strictly_separated(tmp_path: Path) -> None:
    state, paths, config = runtime(tmp_path)
    try:
        _, source_id = add_person(state)
        cutoff = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
        add_observation(
            state,
            source_id=source_id,
            observed_at=cutoff - timedelta(hours=2),
            decision_status="changed",
            delta=paper_delta("Concrete Paper, NeurIPS 2026"),
            suffix="paper",
        )
        add_observation(
            state,
            source_id=source_id,
            observed_at=cutoff - timedelta(hours=1),
            decision_status="source_issue",
            summary="HTTP 429 parser error while scanning",
            suffix="error",
        )
        materialize_window_events(
            state,
            window_start=(cutoff - timedelta(days=1)).isoformat(),
            window_end=cutoff.isoformat(),
        )
        approve_pending_papers(state)
        prepared = prepare_digest(state, paths, config, output_kind="daily", at=cutoff)
        public = prepared["report"]
        developer = prepared["developer"]["report"]
        assert "Concrete Paper, NeurIPS 2026" in public
        assert "来源" in public
        for forbidden in ("HTTP 429", "parser", "异常", "候选", "扫描", "覆盖率"):
            assert forbidden not in public
        assert "HTTP 429 parser error while scanning" in developer
        assert prepared["delivery"]["audience"] == "public"
        assert prepared["developer"]["delivery"]["audience"] == "developer"

        result = deliver_digest(
            state,
            paths,
            config,
            output_kind="daily",
            apply=False,
            lark=None,
        )
        assert len(result["actions"]) == 1
        assert result["actions"][0]["audience"] == "public"
        assert result["actions"][0]["target"]["target_id"] == "public-only"
        assert "Concrete Paper" in result["actions"][0]["markdown"]
        assert "HTTP 429" not in result["actions"][0]["markdown"]
    finally:
        state.close()


def test_low_value_year_description_and_ui_changes_are_suppressed() -> None:
    year = classify_delta_item(
        action="modification",
        source_kind="homepage",
        item={
            "category": "position",
            "before": "I am a first-year PhD student at MIT.",
            "after": "I am a second-year PhD student at MIT.",
        },
    )
    description = classify_delta_item(
        action="modification",
        source_kind="linkedin",
        item={
            "category": "position",
            "before": "Research Intern; built model A",
            "after": "Research Intern; built model B",
            "changed_fields": ["description"],
        },
    )
    button = classify_delta_item(
        action="addition",
        source_kind="homepage",
        item={"category": "publication", "text": "VIDEO PAPER"},
    )
    paper = classify_delta_item(
        action="addition",
        source_kind="scholar",
        item={"category": "publication", "text": "A Real New Paper"},
    )
    assert (year["disposition"], year["reason_code"]) == (
        "suppress",
        "academic_year_rollover",
    )
    assert description["reason_code"] == "description_only_edit"
    assert button["reason_code"] == "ui_control_noise"
    assert paper["disposition"] == "publish"


def test_partial_scan_does_not_block_trusted_change_and_cursor_is_exact_once(
    tmp_path: Path,
) -> None:
    state, paths, config = runtime(tmp_path)
    config["outputs"]["public"] = {
        "document": {"enabled": False},
        "message": {"enabled": False},
    }
    try:
        _, source_id = add_person(state)
        first_cutoff = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
        add_observation(
            state,
            source_id=source_id,
            observed_at=first_cutoff - timedelta(minutes=5),
            decision_status="changed",
            delta=paper_delta("Paper From Partial Run"),
            run_status="partial",
            suffix="partial-paper",
        )
        materialize_window_events(
            state,
            window_start=(first_cutoff - timedelta(days=1)).isoformat(),
            window_end=first_cutoff.isoformat(),
        )
        approve_pending_papers(state)
        first = prepare_digest(
            state, paths, config, output_kind="daily", at=first_cutoff
        )
        assert "Paper From Partial Run" in first["report"]
        state.update_delivery(first["delivery_key"], status="completed")
        cursor = state.report_cursor("public", "daily")
        assert cursor and cursor["delivered_through"] == first["window"]["end"]

        second_cutoff = first_cutoff + timedelta(days=1)
        add_observation(
            state,
            source_id=source_id,
            observed_at=first_cutoff + timedelta(hours=1),
            decision_status="changed",
            delta=paper_delta("Next Paper"),
            suffix="next-paper",
        )
        materialize_window_events(
            state,
            window_start=first_cutoff.isoformat(),
            window_end=second_cutoff.isoformat(),
        )
        approve_pending_papers(state)
        second = prepare_digest(
            state, paths, config, output_kind="daily", at=second_cutoff
        )
        assert second["window"]["start"] == first["window"]["end"]
        assert "Next Paper" in second["report"]
        assert "Paper From Partial Run" not in second["report"]
    finally:
        state.close()


def test_agent_review_is_file_bound_strict_and_late_approval_is_not_lost(
    tmp_path: Path,
) -> None:
    state, paths, config = runtime(tmp_path)
    config["outputs"]["public"] = {
        "document": {"enabled": False},
        "message": {"enabled": False},
    }
    try:
        _, source_id = add_person(state)
        cutoff = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
        add_observation(
            state,
            source_id=source_id,
            observed_at=cutoff - timedelta(hours=1),
            decision_status="changed",
            delta={
                "additions": [
                    {"category": "research", "text": "Now studies embodied agents"}
                ],
                "modifications": [],
                "removals": [],
            },
            suffix="grey",
        )
        first = prepare_digest(state, paths, config, output_kind="daily", at=cutoff)
        assert first["stats"]["published_items"] == 0
        review_path = Path(first["review_requests"]["path"])
        assert stat.S_IMODE(review_path.stat().st_mode) == 0o600
        review_bundle = json.loads(review_path.read_text(encoding="utf-8"))
        request = review_bundle["requests"][0]
        event_id = request["evidence"]["event_id"]
        response = {
            "schema_version": PUBLIC_DECISION_SCHEMA,
            "review_snapshot_id": review_bundle["review_snapshot"]["snapshot_id"],
            "decided_by": "execution-agent:test-suite",
            "decisions": [
                {
                    "request_id": request["request_id"],
                    "evidence_hash": request["evidence_hash"],
                    "decision": "publish",
                    "change_type": "research_direction_changed",
                    "headline": "研究方向变化",
                    "what_changed": "研究方向新增：具身智能体",
                    "why_material": "这是明确的新研究方向",
                    "confidence": 0.95,
                    "evidence_ids": [event_id],
                }
            ],
        }
        applied = apply_agent_review_decisions(state, response)
        assert applied["applied"] == 1
        state.update_delivery(first["delivery_key"], status="completed")

        second = prepare_digest(
            state,
            paths,
            config,
            output_kind="daily",
            at=cutoff + timedelta(days=1),
        )
        assert "具身智能体" in second["report"]
        assert second["events"][0]["event_id"] == event_id

        response["decisions"][0]["evidence_hash"] = "wrong"
        with pytest.raises(ValueError, match="evidence_hash"):
            apply_agent_review_decisions(state, response)
    finally:
        state.close()


def roster_record(name: str, record_id: str, url: str) -> dict:
    return {
        "source_record_id": record_id,
        "canonical_name": name,
        "aliases": [],
        "urls": [url],
        "profile": {},
    }


def test_roster_reconcile_tombstones_only_after_complete_snapshot(tmp_path: Path) -> None:
    state, _, _ = runtime(tmp_path)
    record = roster_record("Roster Person", "rec-1", "https://example.invalid/roster")
    try:
        added = state.reconcile_source_records("base:people", [record])
        source_id = added["scan_source_ids"][0]
        assert added["counts"] == {"added": 1}
        partial = state.reconcile_source_records(
            "base:people", [], source_complete=False, page_cursor="next-page"
        )
        assert partial["removals_evaluated"] is False
        assert state.source_roster_records("base:people")[0]["status"] == "active"
        removed = state.reconcile_source_records("base:people", [], source_complete=True)
        assert removed["counts"] == {"removed": 1}
        assert state.source_roster_records("base:people") == []
        assert state.source_roster_records("base:people", include_removed=True)[0]["status"] == "removed"
        source = state.db.execute(
            "SELECT tracking_enabled,snapshot_json FROM sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        assert source["tracking_enabled"] == 0
        restored = state.reconcile_source_records("base:people", [record])
        assert restored["counts"] == {"restored": 1}
        assert state.db.execute(
            "SELECT tracking_enabled FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()[0] == 1
    finally:
        state.close()


def test_host_budget_circuit_persists_and_resets(tmp_path: Path) -> None:
    state, _, _ = runtime(tmp_path)
    try:
        blocked_until = "2026-09-05T00:00:00+00:00"
        opened = state.update_host_budget(
            "scholar.google.com",
            circuit_state="open",
            blocked_until=blocked_until,
            reason="http_429",
            status_code=429,
            retry_after_seconds=3600,
            consecutive_limits=1,
            canary_required=True,
        )
        assert opened["blocked_until"] == blocked_until
        assert opened["canary_required"] is True
        state.close()
        state = PortableState(tmp_path / "state/people.sqlite3")
        assert state.get_host_budget("Scholar.Google.Com")["circuit_state"] == "open"
        reset = state.reset_host_budget("scholar.google.com")
        assert reset["circuit_state"] == "closed"
        assert reset["blocked_until"] is None
        assert reset["canary_required"] is False
    finally:
        state.close()
