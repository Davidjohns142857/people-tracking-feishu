from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import people_tracking_feishu.cli as cli_module
from people_tracking_feishu.cli import command_digest, command_schedule_tick, command_sync
from people_tracking_feishu.config import (
    RuntimePaths,
    normalize_answers,
    secure_write_json,
)
from people_tracking_feishu.state import PortableState


def _paths(tmp_path: Path) -> RuntimePaths:
    paths = RuntimePaths(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        config_file=tmp_path / "config" / "config.json",
        database=tmp_path / "state" / "people.sqlite3",
        reports=tmp_path / "state" / "reports",
        install_state=tmp_path / "config" / "install-state.json",
    )
    paths.ensure()
    return paths


def _enabled_config() -> dict:
    config = normalize_answers(
        {
            "runtime": {"mode": "openclaw"},
            "sources": [],
            "field_mapping": {
                "person_key": "Person Key",
                "record_type": "Record Type",
                "name": "Name",
                "secondary_id": "ID",
                "aliases": "Aliases",
                "school": "School",
                "research_focus": "Research",
                "stage": "Stage",
                "homepage": "Homepage",
                "scholar": "Scholar",
                "github": "GitHub",
                "linkedin": "LinkedIn",
            },
            "master_database": {
                "mode": "existing_base",
                "url": (
                    "https://synthetic.feishu.cn/base/bas_master"
                    "?table=tbl_people&view=viw_filtered"
                ),
                "base_token": "bas_master",
                "people_table_id": "tbl_people",
            },
            "outputs": {
                "public": {
                    "document": {"enabled": False},
                    "message": {"enabled": False},
                },
                "developer": {"local_markdown": {"enabled": True}},
            },
            "schedule": {
                "timezone": "Asia/Shanghai",
                "scan": "daily",
                "daily_digest": "18:00",
                "weekly_digest": "MON 08:30",
            },
        }
    )
    config["state"] = "enabled"
    config["validation"] = {"all_ok": True}
    return config


def _tick_args() -> argparse.Namespace:
    return argparse.Namespace(lark_cli=None)


def _sync_args(*, bridge_input: Path) -> argparse.Namespace:
    return argparse.Namespace(
        lark_cli=None,
        bridge_input=bridge_input,
        master_bridge_results=None,
        apply=True,
        allow_validated=False,
        include_local_sources=True,
    )


def _source_result(request: dict, source_ref: str) -> dict:
    return {
        "all_ok": True,
        "bridge_nonce": request["bridge_nonce"],
        "expected_refs": request["expected_refs"],
        "expected_refs_hash": request["expected_refs_hash"],
        "request_hash": request["request_hash"],
        "sources": [
            {
                "source_ref": source_ref,
                "source_complete": True,
                "base_token": "bas_master",
                "table_id": "tbl_people",
                "schema_fields": [
                    "Person Key",
                    "Record Type",
                    "Name",
                    "ID",
                    "Homepage",
                ],
                "payload": {
                    "records": [
                        {
                            "record_id": "rec_audit",
                            "fields": {
                                "Record Type": "人员",
                                "Name": "Audit Person",
                                "ID": "audit-person-1",
                                "Homepage": "https://example.org/audit-person",
                            },
                        }
                    ]
                },
            }
        ],
    }


def test_digest_bridge_validates_document_url_before_persisting(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    secure_write_json(paths.config_file, _enabled_config())
    state = PortableState(paths.database)
    try:
        state.prepare_delivery(
            key="delivery-safe-url",
            output_kind="daily",
            period="2026-01-01",
            title="test",
            report_path="/tmp/report.md",
            audience="public",
        )
    finally:
        state.close()

    bad = tmp_path / "bad-delivery-result.json"
    secure_write_json(
        bad,
        {"delivery_key": "delivery-safe-url", "doc_url": "javascript:alert(1)"},
    )
    with pytest.raises(ValueError, match="HTTP"):
        command_digest(argparse.Namespace(bridge_results=bad), paths)
    state = PortableState(paths.database)
    try:
        assert state.delivery("delivery-safe-url")["doc_url"] is None
    finally:
        state.close()

    encoded = tmp_path / "encoded-delivery-result.json"
    secure_write_json(
        encoded,
        {
            "delivery_key": "delivery-safe-url",
            "doc_url": "https://docs.invalid/x)\n\n**FORGED**",
        },
    )
    result = command_digest(argparse.Namespace(bridge_results=encoded), paths)
    assert result["delivery"]["doc_url"] == (
        "https://docs.invalid/x%29%20%2A%2AFORGED%2A%2A"
    )


def test_non_scan_tick_refreshes_authoritative_base_without_crawling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config()
    secure_write_json(paths.config_file, config)
    state = PortableState(paths.database)
    try:
        last_scan = datetime.now(timezone.utc).isoformat()
        state.set_meta("scheduler_last_scan_at", last_scan)
    finally:
        state.close()

    developer_calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "scan_due",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a non-scan tick must not crawl any homepage")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "_digest_due_now",
        lambda kind, *args: kind == "daily",
    )
    monkeypatch.setattr(
        cli_module,
        "export_agent_review_requests",
        lambda *args, **kwargs: {"requests": []},
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_digest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an incomplete roster bridge must block the public digest")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_developer_digest",
        lambda *args, **kwargs: developer_calls.append(kwargs["output_kind"])
        or {"delivery": {"status": "completed"}},
    )

    result = command_schedule_tick(_tick_args(), paths)

    assert result["scan_due"] is False
    assert result["roster_bridge_waiting"] is True
    assert result["public_delivery_blocked"] is True
    assert developer_calls == ["daily"]
    assert result["actions"][0]["operation"] == "scan_cadence"
    refresh = next(
        item for item in result["actions"] if item["operation"] == "reconcile_roster"
    )
    assert refresh["result"]["scan_requested"] is False
    bridge_action = refresh["result"]["actions"][0]
    assert bridge_action["operation"] == "read_authoritative_people_roster"
    assert bridge_action["view_id"] is None
    state = PortableState(paths.database)
    try:
        assert state.get_meta("scheduler_last_scan_at") == last_scan
        pending = state.get_meta("bridge_request:source_sync")
        assert pending["status"] == "pending"
        assert pending["expected_refs"] == [
            config["master_database"]["authoritative_source_ref"]
        ]
    finally:
        state.close()


def test_completed_non_scan_roster_bridge_resumes_once_without_requeue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config()
    secure_write_json(paths.config_file, config)
    state = PortableState(paths.database)
    try:
        state.set_meta("scheduler_last_scan_at", datetime.now(timezone.utc).isoformat())
    finally:
        state.close()

    monkeypatch.setattr(cli_module, "_digest_due_now", lambda *args, **kwargs: False)

    first = command_schedule_tick(_tick_args(), paths)
    bridge = next(
        item["result"]
        for item in first["actions"]
        if item["operation"] == "reconcile_roster"
    )
    source_ref = config["master_database"]["authoritative_source_ref"]
    result_file = tmp_path / "source-result.json"
    secure_write_json(result_file, _source_result(bridge["bridge_request"], source_ref))
    command_sync(_sync_args(bridge_input=result_file), paths)

    monkeypatch.setattr(
        cli_module,
        "command_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a bridge-completed roster must resume from its checkpoint")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "scan_due",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resuming a non-scan tick must not crawl")
        ),
    )

    resumed = command_schedule_tick(_tick_args(), paths)

    assert resumed["scan_due"] is False
    assert resumed["roster_bridge_waiting"] is False
    # command_sync also stages the read-before-write master mirror.  That
    # independent write bridge remains a public gate, but must not cause a
    # second authoritative read to be queued.
    assert resumed["master_bridge_waiting"] is True
    assert resumed["public_delivery_blocked"] is True
    assert any(
        item.get("status") == "reused_fresh_checkpoint"
        for item in resumed["actions"]
        if item["operation"] == "reconcile_roster"
    )
    state = PortableState(paths.database)
    try:
        assert state.get_meta("bridge_request:source_sync")["status"] == "consumed"
        assert state.get_meta("scheduler_roster_checkpoint")["status"] == "ready"
    finally:
        state.close()


def test_expired_roster_checkpoint_cannot_publish_on_a_later_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config()
    secure_write_json(paths.config_file, config)
    now = datetime.now(timezone.utc)
    nonce = "audit-expired-roster-nonce"
    request_hash = "f" * 64
    source_ref = config["master_database"]["authoritative_source_ref"]
    state = PortableState(paths.database)
    try:
        state.set_meta("scheduler_last_scan_at", now.isoformat())
        state.set_meta(
            "bridge_request:source_sync",
            {
                "purpose": "source_sync",
                "status": "consumed",
                "bridge_nonce": nonce,
                "request_hash": request_hash,
            },
        )
        state.set_meta(
            "scheduler_roster_checkpoint",
            {
                "status": "ready",
                "completed_at": (now - timedelta(minutes=11)).isoformat(),
                "config_hash": cli_module._roster_checkpoint_config_hash(config),
                "all_sources_complete": True,
                "authoritative_import": {
                    "source_ref": source_ref,
                    "source_complete": True,
                    "removals_evaluated": True,
                },
                "imports": [],
                "scan_source_ids": [],
                "workflow_id": "roster_" + cli_module.stable_hash(nonce, 32),
                "bridge_nonce": nonce,
                "bridge_request_hash": request_hash,
            },
        )
    finally:
        state.close()

    monkeypatch.setattr(cli_module, "_digest_due_now", lambda kind, *args: kind == "daily")
    monkeypatch.setattr(
        cli_module,
        "export_agent_review_requests",
        lambda *args, **kwargs: {"requests": []},
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_digest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an expired roster checkpoint must block public delivery")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_developer_digest",
        lambda *args, **kwargs: {"delivery": {"status": "completed"}},
    )

    result = command_schedule_tick(_tick_args(), paths)

    assert result["scan_due"] is False
    assert result["roster_bridge_waiting"] is True
    assert result["public_delivery_blocked"] is True
    refresh = next(
        item for item in result["actions"] if item["operation"] == "reconcile_roster"
    )
    assert refresh["status"] == "waiting_for_source_bridge"


def test_failed_scan_blocks_public_but_delivers_developer_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config()
    secure_write_json(paths.config_file, config)
    source_ref = config["master_database"]["authoritative_source_ref"]
    monkeypatch.setattr(
        cli_module,
        "command_sync",
        lambda *args, **kwargs: {
            "apply": True,
            "imports": [
                {
                    "source_ref": source_ref,
                    "source_complete": True,
                    "removals_evaluated": True,
                }
            ],
            "scan_source_ids": [],
        },
    )
    monkeypatch.setattr(
        cli_module,
        "scan_due",
        lambda *args, **kwargs: {
            "validation": {"passed": False, "reasons": ["coverage_failed"]}
        },
    )
    monkeypatch.setattr(
        cli_module,
        "_sync_or_queue_visible_master",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed scan evidence must not be mirrored as a normal result")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "_digest_due_now",
        lambda kind, *args: kind == "daily",
    )
    monkeypatch.setattr(
        cli_module,
        "export_agent_review_requests",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed evidence must not create a public review bundle")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_digest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed evidence must never reach the public report path")
        ),
    )
    developer_calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "deliver_developer_digest",
        lambda *args, **kwargs: developer_calls.append(kwargs["output_kind"])
        or {"delivery": {"status": "completed"}},
    )

    result = command_schedule_tick(_tick_args(), paths)

    assert result["validation_failed"] is True
    assert result["public_delivery_blocked"] is True
    assert developer_calls == ["daily"]
    developer_action = next(
        item
        for item in result["actions"]
        if item["operation"] == "digest_daily_developer"
    )
    assert developer_action["public"]["status"] == "blocked_by_scan_validation"
    state = PortableState(paths.database)
    try:
        assert state.get_meta("scheduler_digest_daily") is None
    finally:
        state.close()
