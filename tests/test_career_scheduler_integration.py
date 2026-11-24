from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from people_tracking_feishu import cli, reporting
from people_tracking_feishu.digest_editor import editorial_request, render_tiered_message
from people_tracking_feishu.state import PortableState
from test_reporting_v09 import add_observation, add_person, runtime


def test_career_candidate_leases_review_before_digest_and_is_projected_once(
    tmp_path, monkeypatch
):
    state, paths, config = runtime(tmp_path)
    now = datetime.now(timezone.utc)
    config["runtime"] = {"mode": "openclaw"}
    config["schedule"]["scan"] = "weekly"
    _, source_id = add_person(state)
    item = {
        "category": "position",
        "before": "Researcher at Acme",
        "after": "Co-founder and CEO at NewCo",
        "changed_fields": ["title", "company"],
    }
    paper = {"category": "publication", "text": "Synthetic Paper"}
    signal = {
        "signal_id": "synthetic-career-occurrence",
        "action": "modification",
        "item": item,
        "first_observed_at": (now - timedelta(minutes=1)).isoformat(),
    }
    add_observation(
        state, source_id=source_id, observed_at=now - timedelta(minutes=1),
        decision_status="candidate",
        delta={"additions": [paper], "modifications": [item],
               "actionable_signals": [signal], "routed_career_items": [signal]},
    )
    state.db.execute("UPDATE observations SET health_status='healthy'")
    state.db.commit()
    state.set_meta("scheduler_last_scan_at", now.isoformat())
    state.close()
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_digest_due_now", lambda *args: False)
    monkeypatch.setattr(cli, "has_due_source_work", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli, "_queue_scheduled_authoritative_roster_refresh", lambda *args: None)
    result = cli.command_schedule_tick(argparse.Namespace(lark_cli=None), paths)
    action = next(a for a in result["actions"] if a["operation"] == "execution_agent_review")
    bundle = json.loads(Path(action["payload_file"]).read_text())
    assert action["kind"] == "career"
    assert len(bundle["requests"]) == 1
    assert "NewCo" in bundle["requests"][0]["evidence"]["candidate_fact"]["after"]

    state = PortableState(paths.database)
    try:
        add_observation(
            state, source_id=source_id, observed_at=now,
            decision_status="changed", suffix="2",
            delta={"additions": [paper], "modifications": [item],
                   "routed_career_items": [signal]},
        )
        for _ in range(2):
            reporting.materialize_window_events(
                state, window_start=(now - timedelta(days=1)).isoformat(), window_end=now.isoformat()
            )
        events = state.events(
            audience="public", window_start=(now - timedelta(days=1)).isoformat(),
            window_end=now.isoformat(),
        )
        assert len(events) == 2
        assert sum(e["event_id"].startswith("evt_career_") for e in events) == 1
        assert {event["disposition"] for event in events} == {"pending_review"}
    finally:
        state.close()


def test_scheduled_search_requires_fact_review_then_enters_weekly_with_source(
    tmp_path, monkeypatch
):
    state, paths, config = runtime(tmp_path)
    now = datetime.now(timezone.utc)
    config["runtime"] = {"mode": "openclaw"}
    config["schedule"]["scan"] = "weekly"
    person_key, source_id = add_person(state)
    add_observation(
        state, source_id=source_id, observed_at=now - timedelta(days=1),
        decision_status="changed",
        delta={"additions": [{"category": "project", "text": "Public project milestone"}]},
    )
    state.set_meta("scheduler_last_scan_at", now.isoformat())
    state.close()
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_digest_due_now", lambda *args: False)
    monkeypatch.setattr(cli, "has_due_source_work", lambda *args, **kwargs: False)
    tick_args = argparse.Namespace(lark_cli=None)
    result = cli.command_schedule_tick(tick_args, paths)
    action = next(a for a in result["actions"] if a["operation"] == "execution_agent_career_search")
    request = json.loads(Path(action["payload_file"]).read_text())
    assert [entry["person_key"] for entry in request["people"]] == [person_key]
    response = {key: request[key] for key in ("schema_version", "window_key", "request_id", "evidence_hash")}
    response.update(decided_by="execution-agent:test", results=[{
        "person_key": person_key, "status": "searched",
        "queries": ["Alice NewCo founder"], "reason": "Matched first-party team announcement.",
        "findings": [{
            "event_type": "founded_company", "organization": "NewCo", "role": "CEO",
            "previous_organization": None, "previous_role": None,
            "event_date": (now - timedelta(days=2)).date().isoformat(),
            "date_precision": "day", "date_semantics": "announcement",
            "source_url": "https://newco.example/announcements/founders",
            "source_type": "company_official",
            "evidence_quote": "Alice co-founded NewCo and serves as CEO.",
            "identity_evidence": "Company announcement links to the tracked homepage.",
            "summary": "NewCo 公告确认 Alice 为联合创始人兼 CEO。",
            "confidence": 0.95, "same_as_event_id": None,
        }],
    }])
    input_file = tmp_path / "career-search-result.json"
    input_file.write_text(json.dumps(response))
    applied = cli.command_career_search_apply(argparse.Namespace(input=input_file), paths)
    assert applied["pending_review"] == 1
    result = cli.command_schedule_tick(tick_args, paths)
    action = next(a for a in result["actions"] if a["operation"] == "execution_agent_review")
    bundle = json.loads(Path(action["payload_file"]).read_text())
    review = bundle["requests"][0]
    assert review["evidence"]["career_search"]["date_semantics"] == "announcement"
    state = PortableState(paths.database)
    try:
        cutoff = datetime.now(timezone.utc)
        assert not state.unreported_public_events(through=cutoff.isoformat(), output_kind="weekly")
        decision = {
            "schema_version": bundle["schema_version"],
            "review_snapshot_id": bundle["review_snapshot"]["snapshot_id"],
            "decided_by": "execution-agent:test",
            "decisions": [{
                "request_id": review["request_id"], "evidence_hash": review["evidence_hash"],
                "decision": "publish", "change_type": "career_founded_company",
                "headline": "联合创立 NewCo", "what_changed": "Alice 为 NewCo 联合创始人兼 CEO。",
                "why_material": "公开公告核实身份及创业事实。", "confidence": 0.95,
                "evidence_ids": [review["evidence"]["event_id"]],
            }],
        }
        approved_fact = decision["decisions"][0]
        valid_detail = approved_fact["what_changed"]
        approved_fact["what_changed"] = "Alice 本周加入 NewCo。"
        with pytest.raises(ValueError, match="evidenced dates"):
            reporting.apply_agent_review_decisions(state, decision)
        approved_fact["what_changed"] = valid_detail
        reporting.apply_agent_review_decisions(state, decision)
        daily = state.unreported_public_events(through=cutoff.isoformat(), output_kind="daily")
        weekly = state.unreported_public_events(through=cutoff.isoformat(), output_kind="weekly")
        assert not daily and len(weekly) == 1
        window = reporting.resolve_report_window(
            state, audience="public", output_kind="weekly", timezone_name="Asia/Shanghai", at=cutoff
        )
        markdown = reporting.render_public_report(window, weekly)
        assert "[公开搜索来源](https://newco.example/announcements/founders)" in markdown
        label = "公告 " + response["results"][0]["findings"][0]["event_date"]
        assert label in markdown
        editorial = editorial_request({"delivery_key": "test", "events": weekly})
        assert editorial["events"][0]["career_search"]["date_semantics"] == "announcement"
        text = render_tiered_message(
            weekly, [{"event_id": weekly[0]["event_id"], "importance": 95,
                      "summary": valid_detail, "award_note": ""}],
            "https://example.org/report",
        )
        assert label in text and "newco.example" in text
    finally:
        state.close()
