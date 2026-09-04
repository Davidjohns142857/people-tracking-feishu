from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from people_tracking_feishu.reporting import (
    PUBLIC_DECISION_SCHEMA,
    _review_request_payload,
    apply_agent_review_decisions,
    build_agent_review_bundle,
    materialize_window_events,
)
from people_tracking_feishu.state import PortableState


def pending_request(state: PortableState, suffix: str) -> dict:
    event_id = f"evt_sig_{suffix}"
    event = state.record_event(
        event_id=event_id,
        audience="public",
        event_type="person_signal",
        occurred_at=f"2026-09-04T0{suffix}:00:00+00:00",
        observation_id=f"obs_{suffix}",
        run_id=f"run_{suffix}",
        source_id=f"src_{suffix}",
        person_key=f"person_{suffix}",
        disposition="pending_review",
        reason_code="grey_zone_requires_execution_agent",
        payload={
            "person_name": f"Person {suffix}",
            "source_kind": "homepage",
            "source_url": f"https://example.invalid/{suffix}",
            "observed_at": f"2026-09-04T0{suffix}:00:00+00:00",
            "action": "addition",
            "raw_item": {
                "category": "research",
                "text": f"Research direction {suffix}",
            },
            "change_type": "research_added",
            "headline": "研究方向变化",
            "what_changed": f"新增研究方向 {suffix}",
            "why_material": "",
            "editorial_reason": "grey_zone_requires_execution_agent",
            "editorial_authority": "deterministic-v0.9",
        },
    )
    evidence_hash, evidence = _review_request_payload(event)
    request_id = f"rev_{suffix}"
    state.prepare_agent_review(
        request_id=request_id,
        event_id=event_id,
        evidence_hash=evidence_hash,
        request={
            "request_id": request_id,
            "evidence_hash": evidence_hash,
            "evidence": evidence,
        },
    )
    return {
        "request_id": request_id,
        "event_id": event_id,
        "evidence_hash": evidence_hash,
    }


def decision(request: dict, *, verdict: str = "publish", marker: str = "") -> dict:
    return {
        "request_id": request["request_id"],
        "evidence_hash": request["evidence_hash"],
        "decision": verdict,
        "change_type": "research_direction_changed",
        "headline": f"研究方向变化{marker}",
        "what_changed": f"新增具身智能研究方向{marker}",
        "why_material": "这是明确且值得关注的新研究方向",
        "confidence": 0.95,
        "evidence_ids": [request["event_id"]],
    }


def response(
    state: PortableState, *decisions: dict, snapshot_id: str | None = None
) -> dict:
    if snapshot_id is None:
        bundle = build_agent_review_bundle(state, batch_size=200)
        snapshot_id = bundle["review_snapshot"]["snapshot_id"]
    return {
        "schema_version": PUBLIC_DECISION_SCHEMA,
        "review_snapshot_id": snapshot_id,
        "decided_by": "execution-agent:atomic-test",
        "decisions": list(decisions),
    }


def assert_all_pending(state: PortableState, requests: list[dict]) -> None:
    for request in requests:
        review = state.db.execute(
            "SELECT status,decision_json FROM agent_review_requests WHERE request_id=?",
            (request["request_id"],),
        ).fetchone()
        assert tuple(review) == ("pending", None)
        assert state.event(request["event_id"])["disposition"] == "pending_review"
    assert state.db.execute(
        "SELECT COUNT(*) FROM event_ledger WHERE event_id LIKE 'evt_dev_agent_%'"
    ).fetchone()[0] == 0


def test_bad_second_hash_rejects_batch_without_partial_write(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "atomic.sqlite3")
    requests = [pending_request(state, "1"), pending_request(state, "2")]
    try:
        second = decision(requests[1])
        second["evidence_hash"] = "0" * 64
        with pytest.raises(ValueError, match="stale|match"):
            apply_agent_review_decisions(
                state, response(state, decision(requests[0]), second)
            )
        assert_all_pending(state, requests)

        unknown = decision(requests[1])
        unknown["request_id"] = "rev_does_not_exist"
        with pytest.raises(ValueError, match="unknown"):
            apply_agent_review_decisions(
                state, response(state, decision(requests[0]), unknown)
            )
        assert_all_pending(state, requests)
    finally:
        state.close()


def test_bad_schema_and_duplicate_request_are_preflight_failures(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "preflight.sqlite3")
    request = pending_request(state, "1")
    try:
        duplicate = decision(request)
        with pytest.raises(ValueError, match="duplicate request_id"):
            apply_agent_review_decisions(state, response(state, duplicate, duplicate))
        assert_all_pending(state, [request])

        malformed = response(state, decision(request))
        malformed["unexpected"] = True
        with pytest.raises(ValueError, match="unexpected top-level"):
            apply_agent_review_decisions(state, malformed)
        assert_all_pending(state, [request])
    finally:
        state.close()


def test_database_error_rolls_back_every_decision(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "rollback.sqlite3")
    requests = [pending_request(state, "1"), pending_request(state, "2")]
    state.db.execute(
        """CREATE TRIGGER fail_second_agent_event
           BEFORE UPDATE ON event_ledger
           WHEN OLD.event_id='evt_sig_2'
           BEGIN SELECT RAISE(ABORT, 'synthetic second write failure'); END"""
    )
    state.db.commit()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic second write failure"):
            apply_agent_review_decisions(
                state, response(state, *(decision(item) for item in requests))
            )
        assert_all_pending(state, requests)
    finally:
        state.close()


def test_exact_replay_is_idempotent_but_changed_replay_is_rejected(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "replay.sqlite3")
    requests = [pending_request(state, "1"), pending_request(state, "2")]
    payload = response(state, *(decision(item) for item in requests))
    try:
        first = apply_agent_review_decisions(state, payload)
        assert first["applied"] == 2
        assert first["replayed"] == 0
        second = apply_agent_review_decisions(state, payload)
        assert second["applied"] == 0
        assert second["replayed"] == 2
        assert state.db.execute(
            "SELECT COUNT(*) FROM event_ledger WHERE event_id LIKE 'evt_dev_agent_%'"
        ).fetchone()[0] == 2

        changed = response(
            state,
            decision(requests[0], marker="不同"),
            decision(requests[1]),
            snapshot_id=payload["review_snapshot_id"],
        )
        with pytest.raises(ValueError, match="different decision"):
            apply_agent_review_decisions(state, changed)
        assert state.event(requests[0]["event_id"])["payload"]["headline"] == "研究方向变化"
    finally:
        state.close()


def test_stale_or_cancelled_evidence_rejects_whole_batch(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "expired.sqlite3")
    stale = pending_request(state, "1")
    valid = pending_request(state, "2")
    try:
        event = state.event(stale["event_id"])
        event["payload"]["raw_item"]["text"] = "Evidence changed after export"
        state.db.execute(
            "UPDATE event_ledger SET payload_json=? WHERE event_id=?",
            (
                json.dumps(event["payload"], ensure_ascii=False, sort_keys=True),
                stale["event_id"],
            ),
        )
        state.db.commit()
        with pytest.raises(ValueError, match="stale"):
            apply_agent_review_decisions(
                state, response(state, decision(valid), decision(stale))
            )
        assert_all_pending(state, [stale, valid])

        state.db.execute(
            "UPDATE agent_review_requests SET status='cancelled' WHERE request_id=?",
            (stale["request_id"],),
        )
        state.db.commit()
        with pytest.raises(ValueError, match="cancelled|expired"):
            apply_agent_review_decisions(state, response(state, decision(stale)))
        assert state.event(valid["event_id"])["disposition"] == "pending_review"
    finally:
        state.close()


def test_deterministic_new_paper_is_only_a_recommendation_until_agent_decides(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "publication-gate.sqlite3")
    try:
        person = state.tracker.add_person(
            "Paper Author", urls=["https://example.invalid/paper-author"]
        )
        source_id = person["sources"][0]["source_id"]
        state.db.execute(
            """INSERT INTO runs(run_id,started_at,completed_at,trigger,status,purpose)
               VALUES('run-paper','2026-09-04T01:00:00+00:00',
                      '2026-09-04T01:05:00+00:00','test','completed','production')"""
        )
        state.db.execute(
            """INSERT INTO observations(
                 observation_id,run_id,source_id,observed_at,health_status,
                 semantic_hash,decision_status,score,delta_json,summary,reviewer
               ) VALUES('obs-paper','run-paper',?,'2026-09-04T01:03:00+00:00',
                        'healthy','hash-paper','changed',0.95,?,
                        'confirmed source change','deterministic')""",
            (
                source_id,
                json.dumps(
                    {
                        "additions": [
                            {
                                "category": "publication",
                                "text": "Error Correction for Reliable Agents",
                                "stable_id": "paper:error-correction",
                            }
                        ],
                        "modifications": [],
                        "removals": [],
                    }
                ),
            ),
        )
        state.db.commit()
        materialize_window_events(
            state,
            window_start="2026-09-04T00:00:00+00:00",
            window_end="2026-09-04T02:00:00+00:00",
        )
        event = state.db.execute(
            "SELECT * FROM event_ledger WHERE audience='public'"
        ).fetchone()
        assert event["disposition"] == "pending_review"
        assert event["reason_code"] == "awaiting_execution_agent"
        assert state.unreported_public_events(
            through="2026-09-04T02:00:00+00:00"
        ) == []
        review = state.pending_agent_reviews()[0]["request"]
        assert review["evidence"]["deterministic_recommendation"] == "publish"
        assert review["evidence"]["deterministic_reason"] == "new_publication"
    finally:
        state.close()


def test_legitimate_paper_and_error_correction_title_is_publishable(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "paper-title.sqlite3")
    request = pending_request(state, "1")
    publish = decision(request)
    publish.update(
        {
            "change_type": "publication_added",
            "headline": "新增论文",
            "what_changed": "新增论文：Error Correction for Reliable Agents",
            "why_material": "这是一项具体的新论文成果",
        }
    )
    try:
        result = apply_agent_review_decisions(state, response(state, publish))
        assert result["applied"] == 1
        event = state.event(request["event_id"])
        assert event["disposition"] == "publish"
        assert "Error Correction" in event["payload"]["what_changed"]
    finally:
        state.close()


def test_deepseek_as_an_employer_or_research_subject_is_publishable(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "deepseek-entity.sqlite3")
    request = pending_request(state, "entity")
    publish = decision(request)
    publish.update(
        {
            "change_type": "affiliation_changed",
            "headline": "任职单位变化",
            "what_changed": "任职单位：某高校 → DeepSeek",
            "why_material": "研究人员加入新的机构",
        }
    )
    try:
        result = apply_agent_review_decisions(state, response(state, publish))
        assert result["applied"] == 1
        event = state.event(request["event_id"])
        assert event["disposition"] == "publish"
        assert "DeepSeek" in event["payload"]["what_changed"]
    finally:
        state.close()


def test_deepseek_api_as_a_legitimate_paper_title_is_publishable(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "deepseek-api-paper.sqlite3")
    request = pending_request(state, "3")
    publish = decision(request)
    publish.update(
        {
            "change_type": "publication_added",
            "headline": "新增论文",
            "what_changed": "新增论文：A Study of the DeepSeek API Ecosystem",
            "why_material": "这是一项具体的新论文成果，不是运行时诊断",
        }
    )
    try:
        result = apply_agent_review_decisions(state, response(state, publish))
        assert result["applied"] == 1
        event = state.event(request["event_id"])
        assert event["disposition"] == "publish"
        assert "DeepSeek API" in event["payload"]["what_changed"]
    finally:
        state.close()
