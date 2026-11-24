from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from people_intel.light_tracker import FetchObservation
from people_tracking_feishu import validation_profile as profile


def test_real_http_controlled_homepage_and_persisted_ground_truth(tmp_path):
    result = profile.run_local_http_smoke(tmp_path / "controlled")
    assert result["local_http_verified"] is True
    assert result["remote_registration_verified"] is False
    assert result["evaluation"]["failed"] == 0
    assert result["evaluation"]["pending"] == 0
    assert {r["scenario"] for r in result["evaluation"]["results"]} == set(profile.SCENARIOS)
    for event in result["evaluation"]["results"][:3]:
        assert event["discovery_delay_seconds"] >= 0
        assert event["confirmation_delay_seconds"] >= event["discovery_delay_seconds"]
    root = tmp_path / "controlled"
    public = (root / "public/index.html").read_text()
    assert "[TEST]" in public and "模拟测试" in public
    assert "mailto:" not in public and "15201633347" not in public
    assert list((root / "public").iterdir()) == [root / "public/index.html"]
    registration = profile.export_registration(root)
    assert registration["record"]["Record Type"] == "validation_profile"
    assert registration["registered_in_remote_list"] is False
    # Persisted evidence remains usable after HTTP server and SQLite have closed.
    assert profile.evaluate_profile(root)["failed"] == 0


def test_missing_or_delayed_collection_fails_sla_instead_of_reporting_success(tmp_path):
    root = tmp_path / "controlled"
    profile.init_profile(root, subject_name="Synthetic User", detection_sla_seconds=10, confirmation_sla_seconds=20)
    event = profile.advance_profile(root)
    later = (datetime.fromisoformat(event["injected_at"]) + timedelta(seconds=21)).isoformat()
    report = profile.evaluate_profile(root, at=later)
    assert report["failed"] == 1
    assert report["results"][0]["failures"] == ["discovery_sla_exceeded", "confirmation_sla_exceeded"]
    assert report["results"][0]["first_detected_at"] is None


def test_hash_chained_injection_log_rejects_rewritten_ground_truth(tmp_path):
    root = tmp_path / "controlled"
    profile.init_profile(root, subject_name="Synthetic User")
    profile.advance_profile(root)
    path = root / "injections.jsonl"
    path.write_text(path.read_text().replace('"scenario": "current"', '"scenario": "undated"'))
    with pytest.raises(ValueError, match="hash chain"):
        profile.evaluate_profile(root)


def test_tick_blocks_without_public_url_and_does_not_mutate_or_claim_remote_pass(tmp_path):
    root = tmp_path / "controlled"
    profile.init_profile(root, subject_name="Synthetic User", interval_seconds=1)
    with pytest.raises(ValueError, match="deployed public URL"):
        profile.tick_profile(root)
    assert not (root / "injections.jsonl").exists()
    assert profile.export_registration(root)["deployment"]["public_url_required"] is True


def test_periodic_tick_waits_for_previous_confirmation(tmp_path, monkeypatch):
    root = tmp_path / "controlled"
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(profile, "_now", lambda: clock[0].isoformat())
    profile.init_profile(root, subject_name="Synthetic User", public_url="https://validation.example.org/", interval_seconds=1)

    def local_fixture_fetch(source):
        return FetchObservation(body=(root / "public/index.html").read_bytes(),
                                final_url=source["url"], observed_at=clock[0].isoformat())

    monkeypatch.setattr(profile, "_fetch", local_fixture_fetch)
    baseline = profile.tick_profile(root)
    assert baseline["observation"]["decision"]["status"] == "baseline"
    assert baseline["injection"] is None
    clock[0] += timedelta(seconds=2)
    candidate = profile.tick_profile(root)
    assert candidate["injection"]["scenario"] == "current"
    assert candidate["observation"]["decision"]["status"] == "candidate"
    clock[0] += timedelta(seconds=2)
    confirmation = profile.tick_profile(root)
    assert confirmation["injection"] is None
    assert confirmation["injection_waiting_for_previous_confirmation"] is True
    assert confirmation["observation"]["decision"]["status"] == "changed"
    clock[0] += timedelta(seconds=2)
    next_event = profile.tick_profile(root)
    assert next_event["injection"]["scenario"] == "historical_backfill"
    assert len(profile._records(root / "injections.jsonl")) == 2


def test_test_data_cannot_be_initialized_over_existing_content(tmp_path):
    root = tmp_path / "occupied"
    root.mkdir()
    (root / "real.sqlite3").write_text("do not touch")
    with pytest.raises(ValueError, match="new empty directory"):
        profile.init_profile(root, subject_name="Synthetic User")
    assert (root / "real.sqlite3").read_text() == "do not touch"


def test_remote_evidence_is_separate_from_self_test_and_bound_to_test_registration(tmp_path):
    root = tmp_path / "controlled"
    url = "https://validation.example.org/"
    profile.init_profile(root, subject_name="Synthetic User", public_url=url)
    event = profile.advance_profile(root)
    at = event["injected_at"]
    receipt = {"record_id": "rec-test", "source_id": "src-test", "source_url": url,
               "readback_at": at, "evidence_ref": "remote-readback-001"}
    profile.record_registration_receipt(root, receipt)
    profile.record_publication_receipt(root, {"event_id": event["event_id"], "published_at": at,
                                             "source_url": url, "evidence_ref": "origin-readback-001"})
    evidence = {"observation_id": "obs-test", "run_id": "run-test", "source_id": "src-test",
                "source_url": url, "record_id": "rec-test", "observed_at": at,
                "decision": {"status": "changed", "health_status": "healthy",
                             "additions": [{"category": "publication", "text": event["title"]}]},
                "evidence_ref": "target-runner:obs-test"}
    assert profile.import_remote_observations(root, [evidence])["imported"] == 1
    assert profile.import_remote_observations(root, [evidence])["imported"] == 0
    assert profile.evaluate_profile(root)["results"][0]["status"] == "pending"
    remote = profile.evaluate_profile(root, evidence_source="remote")
    assert remote["results"][0]["status"] == "passed"
    assert remote["remote_registration_verified"] is False
    assert "not independently authenticated" in remote["scope"]
    with pytest.raises(ValueError, match="registered test row"):
        profile.import_remote_observations(root, [{**evidence, "record_id": "rec-real"}])
    with pytest.raises(ValueError, match="changed evidence"):
        profile.import_remote_observations(root, [{**evidence, "run_id": "run-rewritten"}])


def test_private_or_credential_url_cannot_be_exported_as_remote_control(tmp_path):
    for index, url in enumerate(("http://127.0.0.1/", "https://user:secret@example.org/", "https://example.org/?token=secret")):
        with pytest.raises(ValueError):
            profile.init_profile(tmp_path / str(index), subject_name="Synthetic User", public_url=url)


def remote_setup(root, *, scenario="current"):
    url = "https://validation.example.org/"
    profile.init_profile(root, subject_name="Synthetic User", public_url=url)
    event = profile.advance_profile(root, scenario=scenario)
    profile.record_registration_receipt(root, {
        "record_id": "rec-test", "source_id": "src-test", "source_url": url,
        "readback_at": event["injected_at"], "evidence_ref": "remote-readback",
    })
    profile.record_publication_receipt(root, {
        "event_id": event["event_id"], "published_at": event["injected_at"],
        "source_url": url, "evidence_ref": "origin-readback",
    })
    return event, {
        "observation_id": "obs-test", "run_id": "run-test", "source_id": "src-test",
        "source_url": url, "record_id": "rec-test", "observed_at": event["injected_at"],
        "decision": {"status": "changed", "health_status": "healthy",
                     "additions": [{"category": "publication", "text": event["title"]}]},
        "evidence_ref": "target-runner:obs-test",
    }


def test_remote_summary_marker_and_future_observation_cannot_fake_success(tmp_path):
    root = tmp_path / "controlled"
    event, observation = remote_setup(root)
    now = datetime.fromisoformat(event["injected_at"]) + timedelta(seconds=1)
    unrelated = {**observation, "decision": {"status": "changed", "health_status": "healthy",
                                            "summary": event["event_id"], "additions": []}}
    future = {**observation, "observation_id": "obs-future", "observed_at": (now + timedelta(seconds=1)).isoformat()}
    profile.import_remote_observations(root, [unrelated, future])
    report = profile.evaluate_profile(root, at=now.isoformat(), evidence_source="remote")
    assert report["results"][0]["status"] == "pending"
    assert report["results"][0]["confirmed_at"] is None


def test_remote_delay_uses_chronological_healthy_fact_evidence(tmp_path):
    root = tmp_path / "controlled"
    event, observation = remote_setup(root)
    start = datetime.fromisoformat(event["injected_at"])
    changed = {**observation, "observed_at": (start + timedelta(seconds=10)).isoformat()}
    candidate = {**observation, "observation_id": "obs-earlier",
                 "observed_at": (start + timedelta(seconds=5)).isoformat(),
                 "decision": {**observation["decision"], "status": "candidate"}}
    profile.import_remote_observations(root, [changed, candidate])
    report = profile.evaluate_profile(root, at=(start + timedelta(seconds=11)).isoformat(), evidence_source="remote")
    result = report["results"][0]
    assert result["discovery_delay_seconds"] == 5
    assert result["confirmation_delay_seconds"] == 10
    assert result["status"] == "passed"


def test_no_change_cannot_pass_when_remote_page_was_not_observed(tmp_path):
    root = tmp_path / "controlled"
    event, observation = remote_setup(root, scenario="no_change")
    profile.import_remote_observations(root, [{
        **observation, "decision": {"status": "source_issue", "health_status": "degraded", "summary": "HTTP 503"},
    }])
    after_sla = datetime.fromisoformat(event["injected_at"]) + timedelta(hours=3)
    report = profile.evaluate_profile(root, at=after_sla.isoformat(), evidence_source="remote")
    assert report["results"][0]["status"] == "failed"
    assert report["results"][0]["failures"] == ["no_healthy_comparable_observation"]


def test_unpublished_or_empty_remote_validation_is_explicitly_not_ready(tmp_path):
    root = tmp_path / "controlled"
    profile.init_profile(root, subject_name="Synthetic User", public_url="https://validation.example.org/")
    report = profile.evaluate_profile(root, evidence_source="remote")
    assert report["readiness"] == "blocked" and report["status"] != "passed"
    event = profile.advance_profile(root)
    report = profile.evaluate_profile(root, evidence_source="remote")
    assert report["results"][0]["pending_reason"] == "awaiting_publication_receipt"
    assert report["results"][0]["published_at"] is None


def test_remote_tick_never_collects_instead_of_target_scheduler(tmp_path, monkeypatch):
    root = tmp_path / "controlled"
    event, observation = remote_setup(root)
    clock = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(profile, "_now", lambda: clock.isoformat())

    def forbidden_fetch(_):
        pytest.fail("remote tick must not substitute local fetch for production scheduler")

    monkeypatch.setattr(profile, "_fetch", forbidden_fetch)
    profile.import_remote_observations(root, [{**observation, "decision": {"status": "baseline", "health_status": "healthy"}}])
    result = profile.tick_profile(root, evidence_source="remote")
    assert result["observation"] is None and result["injection"] is None
    assert result["case_failed_stalled"] is True
    assert result["injection_waiting_for_previous_confirmation"] is True
    with pytest.raises(ValueError, match="must use remote"):
        profile.tick_profile(root, evidence_source="local")
