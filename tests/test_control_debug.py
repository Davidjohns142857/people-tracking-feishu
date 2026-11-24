from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from people_intel.light_tracker import FetchObservation
from people_tracking_feishu import cli, debug_status, reporting, tracking, validation_profile
from people_tracking_feishu.ingest import canonical_records
from people_tracking_feishu.state import PortableState
from test_feishu_portable import _enabled_scan_config


def test_first_delivery_then_twice_weekly_with_frozen_retry(tmp_path):
    paths, config = _enabled_scan_config(tmp_path)
    first_at = datetime.fromisoformat("2026-11-24T15:00:00+08:00")
    target = {"enabled": True, "target_kind": "user", "target_id": "ou_fixture"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "load_config", lambda _: config)
        result = cli.command_debug_status(
            argparse.Namespace(enable=True, target_kind=None, target_id=None, control_source_id=None), paths
        )
        assert result["delivery"]["status"] == "developer_target_missing"
        assert "控制主页待接入" in result["markdown"]
    state = PortableState(paths.database)
    try:
        assert debug_status.prepare(state, config, now=first_at) is None
        state.set_meta(debug_status.SETTINGS, {"enabled": True, "target": target})
        first = debug_status.prepare(state, config, now=first_at)["actions"][0]
        assert first["target"] == target and first["audience"] == "developer"
        later = first_at + timedelta(minutes=15)
        assert debug_status.prepare(state, config, now=later)["actions"] == [first]
        receipt = {
            "delivery_key": first["delivery_key"], "message_id": "om_debug_01",
            "message_hash": first["message_hash"],
        }
        with pytest.raises(ValueError, match="frozen"):
            debug_status.apply_receipt(state, {**receipt, "message_hash": "bad"})
        assert debug_status.apply_receipt(state, receipt)["status"] == "completed"
        assert debug_status.apply_receipt(state, receipt)["message_id"] == "om_debug_01"
        thursday = datetime.fromisoformat("2026-11-26T20:00:00+08:00")
        assert debug_status.prepare(state, config, now=thursday - timedelta(seconds=1)) is None
        due = debug_status.prepare(state, config, now=thursday)["actions"][0]
        assert due["delivery_key"] != first["delivery_key"]
    finally:
        state.close()


def test_scheduled_debug_is_independent_of_authoritative_roster_gate(tmp_path, monkeypatch):
    paths, config = _enabled_scan_config(tmp_path)
    config["runtime"]["mode"] = "openclaw"
    state = PortableState(paths.database)
    state.set_meta(debug_status.SETTINGS, {
        "enabled": True, "target": {"enabled": True, "target_kind": "chat", "target_id": "oc_debug"}
    })
    state.close()
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_run_schedule_tick", lambda *_: {
        "actions": [], "roster_bridge_waiting": True, "public_delivery_blocked": True,
    })
    result = cli.command_schedule_tick(argparse.Namespace(), paths)
    assert result["public_delivery_blocked"] is True
    assert result["actions"][0]["audience"] == "developer"
    assert "roster_bridge_waiting" in result["actions"][0]["markdown"]


@pytest.mark.parametrize("scenario", ["startup", "job_change"])
def test_control_source_uses_real_scan_and_review_without_entering_public_report(
    tmp_path, monkeypatch, scenario
):
    paths, config = _enabled_scan_config(tmp_path)
    root = tmp_path / "profile"
    validation_profile.init_profile(root, subject_name="Control Example")
    registered = validation_profile.bind_origin(root, "https://example.org/debug")
    page = root / "public/index.html"
    state = PortableState(paths.database)
    try:
        rows = canonical_records([{"record_id": "rec_debug", "fields": registered["record"]}], {})
        state.reconcile_source_records("base:public-people", rows, authoritative=True)
        person = state.tracker.list_people()[0]
        sid = person["sources"][0]["source_id"]
        http_status = 200

        def fetch(_source):
            return FetchObservation(body=page.read_bytes() if http_status == 200 else None,
                                    status_code=http_status, final_url="https://example.org/debug")

        monkeypatch.setattr(tracking, "_fetch", fetch)
        start = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert tracking.scan_due(state, paths, config, source_ids=[sid])["outcomes"][0]["decision"] == "baseline"
        event = validation_profile.advance_profile(root, scenario=scenario)
        for _ in range(3):
            tracking.scan_due(state, paths, config, source_ids=[sid])
        http_status = 304
        tracking.scan_due(state, paths, config, source_ids=[sid])
        now = datetime.now(timezone.utc)
        reporting.materialize_window_events(state, window_start=start.isoformat(), window_end=now.isoformat())
        result = debug_status.status(state, now=now, timezone_name="Asia/Shanghai")
        assert result["coverage"]["enabled_people"] == 0
        control = result["control_sources"][0]
        assert control["fetch"]["http_status"] == 304
        assert control["fetch"]["received_version"] is None
        assert control["fetch"]["last_version_receipt"]["version"] == event["event_id"]
        assert control["first_detected_at"] and control["confirmed_at"]
        assert control["fact_review"]["disposition"] == "pending_review"
        remote = debug_status.export_control_observations(state, config, sid)
        assert remote["registration"]["record_id"] == "rec_debug"
        validation_profile.record_registration_receipt(root, remote["registration"])
        assert validation_profile.import_remote_observations(root, remote["observations"])["imported"] == 5
        review = state.pending_agent_reviews()[0]
        assert review["request"]["evidence"]["debug_only"] is True
        # Even after an approval, the public report selector must exclude the fixture.
        state.db.execute("UPDATE event_ledger SET disposition='publish' WHERE event_id=?", (review["event_id"],))
        assert not state.unreported_public_events(through=now.isoformat())
    finally:
        state.close()
