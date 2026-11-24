from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from people_tracking_feishu.career_discovery import (
    SCHEMA,
    apply_weekly_career_search,
    eligible_people,
    prepare_weekly_career_search,
    status_weekly_career_search,
)
from people_tracking_feishu.reporting import (
    PUBLIC_DECISION_SCHEMA,
    apply_agent_review_decisions,
    build_agent_review_bundle,
)
from people_tracking_feishu.state import PortableState


NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path):
    value = PortableState(tmp_path / "career-test.sqlite3")
    yield value
    value.close()


def person(state, name="Alice", activity="2026-11-20"):
    url = f"https://example.org/{name.casefold()}"
    profile = {"activity_evidence": [{"date": activity, "source_url": url, "evidence": "Dated public research announcement"}]} if activity else {}
    return state.tracker.add_person(name, urls=[url], profile=profile)


def finding(**changes):
    return {
        "event_type": "founded_company", "organization": "Acme AI", "role": "Co-founder and CEO",
        "previous_organization": None, "previous_role": None,
        "event_date": "2026-11-20", "date_precision": "day", "date_semantics": "effective",
        "source_url": "https://acme.example/team", "source_type": "company_official",
        "evidence_quote": "Alice co-founded Acme AI on 20 November 2026.",
        "identity_evidence": "The same Alice is linked from her existing public homepage.",
        "summary": "Alice co-founded Acme AI and serves as CEO.", "confidence": .95,
        "same_as_event_id": None, **changes,
    }


def prepare(state, at=NOW, window="2026-11-21", batch_size=20):
    return prepare_weekly_career_search(state, at=at, window_key=window, batch_size=batch_size)


def response(request, *, status="no_result", findings=None):
    return {
        "schema_version": SCHEMA, "window_key": request["window_key"], "request_id": request["request_id"],
        "evidence_hash": request["evidence_hash"], "decided_by": "execution-agent:career-unit-test",
        "results": [{"person_key": p["person_key"], "status": status, "queries": [p["canonical_name"] + " startup joined"], "reason": "Verified the requested public career sources.", "findings": findings or []} for p in request["people"]],
    }


def test_eligibility_uses_grounded_activity_not_check_times_or_unanchored_profiles(state):
    eligible = person(state)
    no_activity = person(state, "Bob", None)
    old = person(state, "Carol", "2024-01-01")
    future = person(state, "Dan", "2027-01-01")
    disabled = person(state, "Eve")
    state.db.execute("UPDATE sources SET last_checked_at=?,next_check_at=NULL", (NOW.isoformat(),))
    state.db.execute("UPDATE sources SET tracking_enabled=0 WHERE person_key=?", (disabled["person_key"],))
    state.db.execute("UPDATE people SET profile_json=? WHERE person_key=?", (json.dumps({"last_activity_at": NOW.isoformat(), "updated_at": NOW.isoformat()}), no_activity["person_key"]))
    state.db.commit()
    actual = eligible_people(state, at=NOW)
    assert [p["person_key"] for p in actual] == [eligible["person_key"]]
    assert actual[0]["activity_evidence"][0]["kind"] == "explicit_activity_evidence"
    assert old["person_key"] not in {p["person_key"] for p in actual}
    assert future["person_key"] not in {p["person_key"] for p in actual}


def test_current_snapshot_dated_fact_and_confirmed_observation_can_ground_activity(state):
    snapshot_person = person(state, "Snapshot", None)
    confirmed_person = person(state, "Confirmed", None)
    state.db.execute("UPDATE sources SET snapshot_json=? WHERE person_key=?", (json.dumps({"items": [{"category": "publication", "text": "Grounded paper", "attributes": {"year": "2026"}}]}), snapshot_person["person_key"]))
    sid = confirmed_person["sources"][0]["source_id"]
    at = (NOW - timedelta(days=1)).isoformat()
    state.db.execute("INSERT INTO runs(run_id,started_at,completed_at,trigger,status,purpose) VALUES('confirmed-run',?,?,'synthetic','completed','production')", (at, at))
    delta = {"additions": [{"category": "publication", "text": "A specific paper"}], "modifications": [], "removals": []}
    state.db.execute("INSERT INTO observations(observation_id,run_id,source_id,observed_at,health_status,semantic_hash,decision_status,score,delta_json,summary,reviewer) VALUES('activity-obs','confirmed-run',?,?,'healthy','hash','changed',.9,?,'synthetic','deterministic')", (sid, at, json.dumps(delta)))
    state.db.commit()
    actual = {p["person_key"]: p for p in eligible_people(state, at=NOW)}
    assert actual[snapshot_person["person_key"]]["activity_evidence"][0]["date_precision"] == "year"
    assert actual[confirmed_person["person_key"]]["activity_evidence"][0]["kind"] == "confirmed_material_observation"


def test_exact_frozen_batches_no_result_and_resumption(state):
    for name in ("Alice", "Bob", "Carol"):
        person(state, name)
    first = prepare(state, batch_size=2)
    retry = prepare(state, at=NOW + timedelta(minutes=10), batch_size=1)
    assert retry["request"] == first["request"]
    assert first["eligible_count"] == 3
    assert len(first["request"]["people"]) == 2
    assert first["request"]["lookback_start"] == (NOW - timedelta(days=365)).isoformat()
    assert first["request"]["search_since"] == (NOW - timedelta(days=14)).isoformat()
    person(state, "LateAddition")
    result = apply_weekly_career_search(state, response(first["request"]), at=NOW)
    assert result["no_result"] == 2 and result["completed_count"] == 2
    second = prepare(state, batch_size=2)
    assert second["eligible_count"] == 3 and len(second["request"]["people"]) == 1
    apply_weekly_career_search(state, response(second["request"]), at=NOW)
    complete = prepare(state)
    assert complete["status"] == "completed" and complete["request"] is None
    assert complete["eligible_count"] == complete["completed_count"] == 3


def test_search_unavailable_backs_off_without_blocking_other_people(state):
    for name in ("Alice", "Bob", "Carol"):
        person(state, name)
    first = prepare(state, batch_size=1)
    failed = response(first["request"], status="search_unavailable")
    failed["results"][0]["queries"] = []
    result = apply_weekly_career_search(state, failed, at=NOW)
    assert result["completed_count"] == 0 and result["search_unavailable"] == 1
    for _ in range(2):
        other = prepare(state, batch_size=1)
        assert other["request"]["people"][0]["person_key"] != first["request"]["people"][0]["person_key"]
        apply_weekly_career_search(state, response(other["request"]), at=NOW)
    waiting = prepare(state)
    assert waiting["status"] == "waiting_for_retry" and waiting["pending_count"] == 1
    assert waiting["request"] is None and waiting["completed_count"] == 2
    resumed = prepare(state, at=NOW + timedelta(hours=6, seconds=1))
    assert resumed["request"]["people"] == first["request"]["people"]
    assert resumed["request"]["request_id"] != first["request"]["request_id"]


def test_findings_stage_normal_fact_review_and_never_auto_publish(state):
    person(state)
    request = prepare(state)["request"]
    result = apply_weekly_career_search(state, response(request, status="searched", findings=[finding()]), at=NOW)
    assert result["pending_review"] == 1 and result["completed_count"] == 1
    event = state.event(result["event_ids"][0])
    assert event["disposition"] == "pending_review" and event["source_id"] is None
    assert event["payload"]["source_kind"] == "career_search"
    assert event["payload"]["career_search"]["event_date"] == "2026-11-20"
    bundle = build_agent_review_bundle(state)
    normal_request = bundle["requests"][0]
    assert normal_request["evidence"]["source_kind"] == "career_search"
    assert "effective/day" in normal_request["evidence"]["candidate_fact"]["text"]
    decision = {"request_id": normal_request["request_id"], "evidence_hash": normal_request["evidence_hash"], "decision": "publish", "change_type": "career_founded_company", "headline": "发现创业信息", "what_changed": "Alice co-founded Acme AI.", "why_material": "Verified identity and the official announcement.", "confidence": .95, "evidence_ids": [event["event_id"]]}
    apply_agent_review_decisions(state, {"schema_version": PUBLIC_DECISION_SCHEMA, "review_snapshot_id": bundle["review_snapshot"]["snapshot_id"], "decided_by": "execution-agent:separate-fact-review", "decisions": [decision]})
    assert state.event(event["event_id"])["disposition"] == "publish"


def test_same_event_multi_article_prefers_original_source_not_response_order(state):
    person(state)
    request = prepare(state)["request"]
    media = finding(organization="Acme AI Inc.", source_url="https://news.example/acme", source_type="reputable_media", event_date="2026", date_precision="year")
    official = finding()
    result = apply_weekly_career_search(state, response(request, status="searched", findings=[media, official]), at=NOW)
    assert result["findings"] == 2 and result["pending_review"] == result["duplicates"] == 1
    assert state.event(result["event_ids"][0])["payload"]["source_url"] == official["source_url"]


def test_different_role_changes_or_nonoverlapping_dates_are_not_deduplicated(state):
    person(state)
    request = prepare(state)["request"]
    variants = [finding(event_type="role_change", role="CTO"), finding(event_type="role_change", role="CFO"), finding(event_date="2026-01-01")]
    result = apply_weekly_career_search(state, response(request, status="searched", findings=variants), at=NOW)
    assert result["pending_review"] == 3 and result["duplicates"] == 0


def test_dedup_prior_reported_regular_fact_and_compact_known_facts(state):
    p = person(state)
    state.record_event(event_id="old-reported", audience="public", event_type="person_signal", occurred_at="2026-11-20T00:00:00Z", disposition="publish", reason_code="execution_agent_publish", person_key=p["person_key"], payload={"person_name": "Alice", "source_kind": "homepage", "source_url": "https://example.org/alice", "what_changed": "Alice co-founded Acme AI.", "change_type": "position_added", "raw_item": {"category": "position", "text": "Alice co-founded Acme AI.", "attributes": {"company": "Acme AI", "title": "Co-founder and CEO", "start_date": "2026-11"}}})
    state.prepare_report(key="old-week", audience="public", output_kind="weekly", period="2026-W47", window_start="2026-11-16T00:00:00Z", window_end="2026-11-23T00:00:00Z", title="Synthetic old report", report_path="synthetic-unused.md", content_hash="synthetic", event_ids=["old-reported"])
    request = prepare(state)["request"]
    known = request["people"][0]["known_career_facts"][0]
    assert known["already_reported"] and known["event_id"] == "old-reported"
    result = apply_weekly_career_search(state, response(request, status="searched", findings=[finding(same_as_event_id="old-reported")]), at=NOW)
    assert result["duplicates"] == 1 and result["pending_review"] == 0
    assert state.event("old-reported")["disposition"] == "publish"


def test_search_facts_dedup_across_windows_and_known_facts_are_compact(state):
    person(state)
    first = prepare(state)["request"]
    apply_weekly_career_search(state, response(first, status="searched", findings=[finding()]), at=NOW)
    second = prepare(state, at=NOW + timedelta(days=7), window="2026-11-28")["request"]
    known = second["people"][0]["known_career_facts"][0]
    assert "evidence_quote" not in known and "identity_evidence" not in known
    result = apply_weekly_career_search(state, response(second, status="searched", findings=[finding(source_url="https://news.example/another-report", source_type="reputable_media")]), at=NOW + timedelta(days=7))
    assert result["duplicates"] == 1 and result["pending_review"] == 0


def test_invalid_partial_batch_has_no_writes_and_exact_replay_is_idempotent(state):
    person(state)
    person(state, "Bob")
    request = prepare(state)["request"]
    bad = response(request)
    bad["results"] = bad["results"][:1]
    with pytest.raises(ValueError, match="every requested person"):
        apply_weekly_career_search(state, bad, at=NOW)
    assert status_weekly_career_search(state, request["window_key"])["completed_count"] == 0
    good = response(request)
    applied = apply_weekly_career_search(state, good, at=NOW)
    replay = apply_weekly_career_search(state, good, at=NOW + timedelta(hours=1))
    assert replay["idempotent_replay"] and replay["completed_count"] == applied["completed_count"] == 2
    changed = copy.deepcopy(good)
    changed["results"][0]["reason"] = "Different decision"
    with pytest.raises(ValueError, match="different response"):
        apply_weekly_career_search(state, changed, at=NOW)


def test_database_failure_rolls_back_batch_jobs_events_and_reviews_together(state):
    person(state)
    request = prepare(state)["request"]
    state.db.execute("CREATE TRIGGER reject_career_review BEFORE INSERT ON agent_review_requests BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    state.db.commit()
    with pytest.raises(Exception, match="synthetic failure"):
        apply_weekly_career_search(state, response(request, status="searched", findings=[finding()]), at=NOW)
    assert state.db.execute("SELECT COUNT(*) FROM event_ledger").fetchone()[0] == 0
    assert state.db.execute("SELECT COUNT(*) FROM agent_review_requests").fetchone()[0] == 0
    assert state.db.execute("SELECT status FROM career_search_batches").fetchone()[0] == "pending"
    assert state.db.execute("SELECT status FROM career_search_people").fetchone()[0] == "inflight"


@pytest.mark.parametrize("changes", [
    {"source_url": "http://127.0.0.1/private"},
    {"source_url": "https://user:password@example.org/"},
    {"source_url": "https://example.org/?access_token=secret"},
    {"confidence": .79}, {"confidence": True},
    {"event_date": "2027-01-01"},
    {"event_date": "2026-11", "date_precision": "day"},
    {"event_date": None, "date_precision": "unknown", "date_semantics": "effective"},
])
def test_invalid_source_date_or_verification_does_not_stage_a_fact(state, changes):
    person(state)
    request = prepare(state)["request"]
    with pytest.raises(ValueError):
        apply_weekly_career_search(state, response(request, status="searched", findings=[finding(**changes)]), at=NOW)
    assert state.db.execute("SELECT COUNT(*) FROM event_ledger").fetchone()[0] == 0


def test_unknown_date_remains_unknown_and_disabled_people_do_not_get_new_events(state):
    p = person(state)
    request = prepare(state)["request"]
    result = apply_weekly_career_search(state, response(request, status="searched", findings=[finding(event_date=None, date_precision="unknown", date_semantics="unknown")]), at=NOW)
    career = state.event(result["event_ids"][0])["payload"]["career_search"]
    assert career["event_date"] is None and career["date_start"] is None and career["date_end"] is None
    second = prepare(state, at=NOW + timedelta(days=7), window="2026-11-28")["request"]
    state.db.execute("UPDATE sources SET tracking_enabled=0 WHERE person_key=?", (p["person_key"],))
    state.db.commit()
    result = apply_weekly_career_search(state, response(second, status="searched", findings=[finding(organization="Another Company")]), at=NOW + timedelta(days=7))
    assert result["skipped_inactive"] == 1 and result["pending_review"] == 0
