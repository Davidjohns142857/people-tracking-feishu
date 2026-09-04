from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import people_tracking_feishu.cli as cli_module
from people_tracking_feishu.cli import (
    _master_expected_refs,
    _new_bridge_request,
    command_bootstrap,
    command_schedule_tick,
    command_sync,
)
from people_tracking_feishu.config import (
    ConfigError,
    RuntimePaths,
    authoritative_roster_source_ref,
    mark_state,
    normalize_answers,
    secure_write_json,
    validate_config,
)
from people_tracking_feishu.scheduler import openclaw_cron_plan
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


def _existing_base_answers(*, runtime: str = "openclaw") -> dict:
    return {
        "runtime": {"mode": runtime},
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
            "url": "https://synthetic.feishu.cn/base/bas_master?table=tbl_people&view=viw_hidden",
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


def _enabled_config(*, runtime: str = "openclaw") -> dict:
    config = normalize_answers(_existing_base_answers(runtime=runtime))
    config["state"] = "enabled"
    config["validation"] = {"all_ok": True}
    return config


def _sync_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "lark_cli": None,
        "bridge_input": None,
        "master_bridge_results": None,
        "apply": True,
        "allow_validated": False,
        "include_local_sources": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_existing_base_is_the_default_authoritative_source_and_deepseek_is_removed() -> None:
    answers = _existing_base_answers()
    answers["apis"] = {
        "deepseek": {
            "enabled": False,
            "key_reference": "sk-stale-inline-value-that-must-never-be-read",
        }
    }
    config = normalize_answers(answers)

    assert config["sources"] == []
    assert config["master_database"]["authoritative_roster"] is True
    assert config["master_database"]["authoritative_source_ref"].startswith(
        "authoritative-base-"
    )
    assert config["field_mapping"]["record_type"] == "Record Type"
    assert "deepseek" not in config["apis"]
    validate_config(config, require_complete=True)

    without_view = _existing_base_answers()
    without_view["master_database"]["url"] = (
        "https://synthetic.feishu.cn/base/bas_master?table=tbl_people&view=another"
    )
    assert authoritative_roster_source_ref(
        normalize_answers(without_view)["master_database"]
    ) == authoritative_roster_source_ref(config["master_database"])

    legacy = json.loads(json.dumps(config))
    legacy["apis"]["deepseek"] = {
        "enabled": False,
        "key_reference": "sk-stale-inline-value-is-tolerated",
    }
    validate_config(legacy, require_complete=True)
    legacy["apis"]["deepseek"]["enabled"] = True
    with pytest.raises(ConfigError, match="DeepSeek review was removed"):
        validate_config(legacy, require_complete=True)

    reordered_answers = _existing_base_answers()
    reordered_answers["sources"] = [
        {"kind": "local_file", "path": "/tmp/one.json"},
        {"kind": "local_file", "path": "/tmp/two.json"},
    ]
    reordered = normalize_answers(reordered_answers)
    original_refs = [source["source_ref"] for source in reordered["sources"]]
    reordered["sources"].reverse()
    validate_config(reordered)
    assert [source["source_ref"] for source in reordered["sources"]] == original_refs[::-1]


class _DirectLark:
    def __init__(self) -> None:
        self.record_reads: list[dict] = []
        self.field_reads: list[tuple[str, str, str]] = []

    def base_schema(self, url: str, **kwargs: object) -> dict:
        return {
            "base_token": "bas_master",
            "selected_table": {"table_id": "tbl_people", "name": "People"},
            "tables": [{"table_id": "tbl_people", "name": "People"}],
            "fields": [
                {"field_name": "Person Key"},
                {"field_name": "Record Type"},
                {"field_name": "Name"},
            ],
        }

    def list_base_records(self, **kwargs: object) -> list[dict]:
        self.record_reads.append(dict(kwargs))
        return [
            {
                "record_id": "rec_resolved",
                "fields": {
                    "Name": "Ada Resolved",
                    "ID": "ada-1",
                    "Homepage": "https://example.org/ada",
                },
            },
            {
                "record_id": "rec_review",
                "fields": {"Name": "Needs Review", "ID": "review-1"},
            },
        ]

    def list_base_fields(self, **kwargs: str) -> list[dict]:
        self.field_reads.append(
            (kwargs["base_token"], kwargs["table_id"], kwargs["identity"])
        )
        return [
            {"field_name": "Person Key"},
            {"field_name": "Record Type"},
            {"field_name": "Name"},
            {"field_name": "Sync Status"},
        ]


def test_direct_authoritative_ingest_ignores_view_passes_schema_and_links_original_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config(runtime="claude_lark_cli")
    secure_write_json(paths.config_file, config)
    lark = _DirectLark()
    sync_calls: list[dict] = []
    authoritative_flags: list[bool] = []

    original_reconcile = PortableState.reconcile_source_records

    def reconcile(self, *args, authoritative: bool = False, **kwargs):
        authoritative_flags.append(authoritative)
        return original_reconcile(self, *args, authoritative=authoritative, **kwargs)

    monkeypatch.setattr(cli_module, "_lark", lambda *args, **kwargs: lark)
    monkeypatch.setattr(PortableState, "reconcile_source_records", reconcile)
    monkeypatch.setattr(
        cli_module,
        "sync_visible_master",
        lambda *args, **kwargs: sync_calls.append(dict(kwargs)) or {"apply": True},
    )

    result = command_sync(_sync_args(), paths)
    source_ref = config["master_database"]["authoritative_source_ref"]
    assert [item["source_ref"] for item in result["imports"]] == [source_ref]
    assert authoritative_flags == [True]
    assert lark.record_reads == [
        {
            "base_token": "bas_master",
            "table_id": "tbl_people",
            "identity": "user",
            "view_id": None,
            "field_names": None,
        }
    ]
    assert sync_calls[0]["authoritative_roster"] is True
    assert sync_calls[0]["available_fields"]["People"] == [
        {"field_name": "Person Key"},
        {"field_name": "Record Type"},
        {"field_name": "Name"},
        {"field_name": "Sync Status"},
    ]

    state = PortableState(paths.database)
    try:
        person = next(
            item for item in state.tracker.list_people() if item["canonical_name"] == "Ada Resolved"
        )
        assert state.feishu_record_link(
            entity_kind="person",
            entity_key=person["person_key"],
            base_token="bas_master",
            table_id="tbl_people",
        ) == "rec_resolved"
        issue = state.db.execute(
            "SELECT issue_id FROM intake_issues WHERE source_record_id='rec_review'"
        ).fetchone()
        assert issue is not None
        assert state.feishu_record_link(
            entity_kind="person",
            entity_key=f"review:{issue['issue_id']}",
            base_token="bas_master",
            table_id="tbl_people",
        ) == "rec_review"
        # A later unchanged/restored reconciliation repairs a stale link instead
        # of creating a second visible row.
        state.set_feishu_record_link(
            entity_kind="person",
            entity_key=person["person_key"],
            base_token="bas_master",
            table_id="tbl_people",
            record_id="rec_stale",
        )
    finally:
        state.close()
    command_sync(_sync_args(), paths)
    state = PortableState(paths.database)
    try:
        assert state.feishu_record_link(
            entity_kind="person",
            entity_key=person["person_key"],
            base_token="bas_master",
            table_id="tbl_people",
        ) == "rec_resolved"
    finally:
        state.close()


def test_openclaw_injects_one_unfiltered_authoritative_roster_action(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    answers = _existing_base_answers()
    answers["sources"] = [
        {
            "kind": "feishu_base",
            "url": "https://synthetic.feishu.cn/base/bas_master?table=tbl_people&view=legacy",
            "table_id": "tbl_people",
            "view_id": "viw_legacy",
        }
    ]
    config = normalize_answers(answers)
    config["state"] = "enabled"
    config["validation"] = {"all_ok": True}
    secure_write_json(paths.config_file, config)

    result = command_sync(_sync_args(), paths)

    assert result["apply"] is False
    assert result["authoritative_roster_pending"] is True
    assert len(result["actions"]) == 1
    action = result["actions"][0]
    assert action["operation"] == "read_authoritative_people_roster"
    assert action["source_ref"] == config["master_database"]["authoritative_source_ref"]
    assert action["view_id"] is None
    assert action["result_requirements"]["source_complete"].startswith("must be true")


def test_schedule_tick_fails_closed_while_authoritative_bridge_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    secure_write_json(paths.config_file, _enabled_config())
    monkeypatch.setattr(
        cli_module,
        "scan_due",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale roster must not be scanned")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_digest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("public digest must not be delivered")
        ),
    )

    result = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)

    assert result["public_delivery_blocked"] is True
    assert result["roster_sync_failed"] is True
    assert all(item["operation"] != "scan_due_only" for item in result["actions"])
    state = PortableState(paths.database)
    try:
        run = state.db.execute(
            "SELECT status,metrics_json FROM runtime_runs WHERE action='roster_sync'"
        ).fetchone()
        assert run["status"] == "failed"
        assert json.loads(run["metrics_json"])["public_delivery_blocked"] is True
    finally:
        state.close()


def test_schedule_tick_also_fails_closed_for_non_master_roster_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    answers = _existing_base_answers()
    answers["master_database"] = {"mode": "local_or_api"}
    answers["sources"] = [
        {"kind": "feishu_doc", "url": "https://synthetic.feishu.cn/docx/roster"}
    ]
    config = normalize_answers(answers)
    config["state"] = "enabled"
    config["validation"] = {"all_ok": True}
    secure_write_json(paths.config_file, config)
    monkeypatch.setattr(
        cli_module,
        "scan_due",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a pending source bridge must block stale scans")
        ),
    )

    result = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)

    assert result["public_delivery_blocked"] is True
    assert result["roster_bridge_waiting"] is True
    assert all(item["operation"] != "scan_due_only" for item in result["actions"])


def test_schedule_tick_writes_scan_results_back_before_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config(runtime="openclaw")
    secure_write_json(paths.config_file, config)
    source_ref = config["master_database"]["authoritative_source_ref"]
    order: list[str] = []

    monkeypatch.setattr(
        cli_module,
        "command_sync",
        lambda *args, **kwargs: order.append("roster")
        or {
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
        lambda *args, **kwargs: order.append("scan")
        or {"validation": {"passed": True}},
    )
    monkeypatch.setattr(
        cli_module,
        "_sync_or_queue_visible_master",
        lambda *args, **kwargs: order.append("master") or {"apply": True},
    )
    monkeypatch.setattr(cli_module, "_digest_due_now", lambda *args, **kwargs: False)

    result = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)

    assert order == ["roster", "scan", "master"]
    assert [item["operation"] for item in result["actions"]] == [
        "reconcile_roster",
        "scan_due_only",
        "sync_visible_master_after_scan",
    ]


def test_source_bridge_is_not_consumed_by_preview_and_consumes_only_after_apply(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    answers = _existing_base_answers()
    answers["master_database"] = {"mode": "local_or_api"}
    answers["sources"] = [
        {"kind": "feishu_doc", "url": "https://synthetic.feishu.cn/docx/roster"}
    ]
    config = normalize_answers(answers)
    config["state"] = "enabled"
    config["validation"] = {"all_ok": True}
    secure_write_json(paths.config_file, config)
    waiting = command_sync(_sync_args(), paths)
    request = waiting["bridge_request"]
    source_ref = config["sources"][0]["source_ref"]
    bridge_file = tmp_path / "source-bridge.json"
    secure_write_json(
        bridge_file,
        {
            "all_ok": True,
            "bridge_nonce": request["bridge_nonce"],
            "expected_refs": request["expected_refs"],
            "expected_refs_hash": request["expected_refs_hash"],
            "request_hash": request["request_hash"],
            "sources": [
                {
                    "source_ref": source_ref,
                    "source_complete": True,
                    "payload": {
                        "records": [
                            {
                                "record_id": "doc-row-1",
                                "Name": "Bridge Person",
                                "ID": "bridge-1",
                                "Homepage": "https://example.org/bridge",
                            }
                        ]
                    },
                }
            ],
        },
    )

    preview = command_sync(_sync_args(bridge_input=bridge_file, apply=False), paths)
    assert preview["apply"] is False
    state = PortableState(paths.database)
    try:
        assert state.get_meta("bridge_request:source_sync")["status"] == "pending"
        assert state.status()["counts"]["people"] == 0
    finally:
        state.close()

    command_sync(_sync_args(bridge_input=bridge_file, apply=True), paths)
    state = PortableState(paths.database)
    try:
        assert state.get_meta("bridge_request:source_sync")["status"] == "consumed"
        assert state.status()["counts"]["people"] == 1
    finally:
        state.close()


def test_schedule_scan_cadence_skips_recent_success_without_advancing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    secure_write_json(paths.config_file, _enabled_config())
    state = PortableState(paths.database)
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        state.set_meta("scheduler_last_scan_at", timestamp)
    finally:
        state.close()
    monkeypatch.setattr(
        cli_module,
        "command_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("roster sync is not due")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "scan_due",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("scan is not due")),
    )
    monkeypatch.setattr(cli_module, "_digest_due_now", lambda *args, **kwargs: False)

    result = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)

    assert result["scan_due"] is False
    assert result["actions"][0]["operation"] == "scan_cadence"
    state = PortableState(paths.database)
    try:
        assert state.get_meta("scheduler_last_scan_at") == timestamp
    finally:
        state.close()


def test_failed_scan_does_not_advance_cadence_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config()
    secure_write_json(paths.config_file, config)
    source_ref = config["master_database"]["authoritative_source_ref"]
    old_timestamp = "2026-01-01T00:00:00+00:00"
    state = PortableState(paths.database)
    try:
        state.set_meta("scheduler_last_scan_at", old_timestamp)
    finally:
        state.close()
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
        lambda *args, **kwargs: {"validation": {"passed": False}},
    )
    monkeypatch.setattr(cli_module, "_digest_due_now", lambda *args, **kwargs: False)

    result = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)

    assert result["validation_failed"] is True
    state = PortableState(paths.database)
    try:
        assert state.get_meta("scheduler_last_scan_at") == old_timestamp
    finally:
        state.close()


def test_pending_public_review_does_not_block_developer_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    secure_write_json(paths.config_file, _enabled_config())
    state = PortableState(paths.database)
    try:
        state.set_meta("scheduler_last_scan_at", datetime.now(timezone.utc).isoformat())
    finally:
        state.close()
    developer_calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_digest_due_now",
        lambda kind, *args: kind == "daily",
    )
    monkeypatch.setattr(
        cli_module,
        "export_agent_review_requests",
        lambda *args, **kwargs: {"requests": [{"request_id": "review-1"}]},
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_developer_digest",
        lambda *args, **kwargs: developer_calls.append(kwargs["output_kind"])
        or {"delivery": {"status": "completed"}},
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_digest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("public digest must wait for the host Agent")
        ),
    )

    result = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)

    assert developer_calls == ["daily"]
    assert result["reviews_waiting"] is True
    assert any(
        item["operation"] == "digest_daily_developer" for item in result["actions"]
    )


def test_openclaw_review_annotation_targets_original_row_without_retyping_it(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    try:
        state.set_feishu_record_link(
            entity_kind="person",
            entity_key="person_1",
            base_token="bas_master",
            table_id="tbl_people",
            record_id="rec_person",
        )
        state.set_feishu_record_link(
            entity_kind="person",
            entity_key="review:iss_1",
            base_token="bas_master",
            table_id="tbl_people",
            record_id="rec_original",
        )
        records = {
            "People": [
                {
                    "entity_key": "person_1",
                    "field_values": {
                        "person_key": "person_1",
                        "record_type": "人员",
                        "name": "Human Name",
                        "sync_status": "正常",
                    },
                },
                {
                    "entity_key": "review:iss_1",
                    "origin_source_ref": "authoritative-source",
                    "origin_record_id": "rec_original",
                    "field_values": {
                        "person_key": "review:iss_1",
                        "record_type": "审核项",
                        "review_status": "待补主页",
                    },
                }
            ],
            "Sources": [],
        }
        safe = cli_module._bridge_safe_master_records(
            state,
            records,
            authoritative_roster=True,
            base_token="bas_master",
            people_table_id="tbl_people",
            authoritative_source_ref="authoritative-source",
            live_records={
                "People": [
                    {
                        "record_id": "rec_person",
                        "fields": {
                            "Name": "Human Name",
                            "Person Key": "person_1",
                            "Record Type": "人员",
                            "Sync Status": "旧状态",
                            "Human Notes": "must survive",
                        },
                    },
                    {
                        "record_id": "rec_original",
                        "fields": {
                            "Name": "Needs Review",
                            "Record Type": "人员",
                            "Human Notes": "keep me",
                        },
                    },
                ]
            },
            field_mapping={
                "People": {
                    "name": "Name",
                    "person_key": "Person Key",
                    "record_type": "Record Type",
                    "sync_status": "Sync Status",
                }
            },
        )
    finally:
        state.close()

    assert records["People"][1]["field_values"]["record_type"] == "审核项"
    person_item = safe["People"][0]
    assert person_item["target_record_id"] == "rec_person"
    assert person_item["field_values"] == {
        "person_key": "person_1",
        "record_type": "人员",
        "sync_status": "正常",
    }
    assert "name" not in person_item["field_values"]
    assert person_item["write_contract"]["expected_before_fields"]["fields"][
        "Human Notes"
    ] == "must survive"
    item = safe["People"][1]
    assert item["target_record_id"] == "rec_original"
    assert item["preserve_authoritative_identity"] is True
    assert item["field_values"]["record_type"] == "人员"
    assert "person_key" not in item["field_values"]
    assert set(item["write_contract"]["machine_patch"]).issubset(
        set(item["write_contract"]["machine_field_allowlist"])
    )


def test_external_review_item_may_create_a_separate_review_row(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    try:
        records = {
            "People": [
                {
                    "entity_key": "review:external_issue",
                    "origin_source_ref": "external-source",
                    "origin_record_id": "external-row-1",
                    "field_values": {
                        "name": "External Review",
                        "person_key": "review:external_issue",
                        "record_type": "审核项",
                        "review_status": "待确认",
                    },
                }
            ],
            "Sources": [],
        }
        safe = cli_module._bridge_safe_master_records(
            state,
            records,
            authoritative_roster=True,
            authoritative_source_ref="authoritative-source",
            base_token="bas_master",
            people_table_id="tbl_people",
        )
    finally:
        state.close()

    item = safe["People"][0]
    assert "target_record_id" not in item
    assert item["write_contract"]["operation"] == "create"
    assert item["field_values"]["record_type"] == "审核项"
    assert item["write_contract"]["create_human_fields"]["name"] == "External Review"


def test_cached_master_pre_read_expires_instead_of_reusing_stale_human_fields(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "stale-master-pre-read.sqlite3")
    records = {
        "People": [
            {
                "record_id": "rec_stale",
                "fields": {"Name": "Human Name", "Human Notes": "old value"},
            }
        ]
    }
    snapshot = {
        "base_token": "bas_master",
        "table_ids": {"People": "tbl_people"},
        "records": records,
        "captured_at": (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat(),
        "snapshot_hash": cli_module._bridge_contract_hash(records),
    }
    try:
        state.set_meta("master_live_pre_read", snapshot)
        assert cli_module._load_master_live_pre_read(
            state,
            base_token="bas_master",
            people_table_id="tbl_people",
            sources_table_id=None,
        ) == {}

        state.set_meta(
            "master_live_pre_read",
            {**snapshot, "captured_at": datetime.now(timezone.utc).isoformat()},
        )
        assert cli_module._load_master_live_pre_read(
            state,
            base_token="bas_master",
            people_table_id="tbl_people",
            sources_table_id=None,
        ) == records
    finally:
        state.close()


def _master_result_payload(request: dict, *, outcomes: list[dict]) -> dict:
    return {
        "schema_version": request["schema_version"],
        "purpose": "master_sync",
        "all_ok": True,
        "write_protocol": "machine-fields-verified-v1",
        "preflight_all_ok": True,
        "bridge_nonce": request["bridge_nonce"],
        "expected_refs": request["expected_refs"],
        "expected_refs_hash": request["expected_refs_hash"],
        "request_hash": request["request_hash"],
        "completed_refs": request["expected_refs"],
        "base_token": "bas_master",
        "base_url": "https://synthetic.feishu.cn/base/bas_master?table=tbl_people",
        "table_ids": {"People": "tbl_people"},
        "schema_fields": {
            "People": ["Person Key", "Record Type", "Name", "Sync Status"]
        },
        "outcomes": outcomes,
    }


def _verified_outcome(
    request: dict,
    entity_key: str,
    record_id: str,
    *,
    table: str = "People",
) -> dict:
    contract = request["expected_writes"][f"{table}:{entity_key}"]
    applied_mapping = {
        semantic: candidates[0]
        for semantic, candidates in contract["machine_field_candidates"].items()
    }
    created_mapping = {
        semantic: contract["human_field_candidates"][semantic][0]
        for semantic in contract["create_human_fields"]
    }
    post_fields = dict(contract["expected_before_fields"]["fields"])
    for semantic, desired in contract["machine_patch"].items():
        post_fields[applied_mapping[semantic]] = desired
    for semantic, desired in contract["create_human_fields"].items():
        post_fields[created_mapping[semantic]] = desired
    post_read = {
        "record_id": record_id,
        "fields": post_fields,
    }
    return {
        "table": table,
        "entity_key": entity_key,
        "record_id": record_id,
        "operation": contract["operation"],
        "target_record_id": contract["target_record_id"],
        "contract_hash": contract["contract_hash"],
        "machine_patch_hash": contract["machine_patch_hash"],
        "expected_before_hash": contract["expected_before_hash"],
        "preflight_ok": True,
        "before_read": contract["expected_before_fields"],
        "applied_patch": contract["machine_patch"],
        "applied_field_mapping": applied_mapping,
        "human_patch": {},
        "created_human_fields": (
            contract["create_human_fields"]
            if contract["operation"] == "create"
            else {}
        ),
        "created_field_mapping": (
            created_mapping if contract["operation"] == "create" else {}
        ),
        "post_read": post_read,
        "post_read_hash": cli_module._bridge_contract_hash(post_read),
        "human_fields_unchanged": True,
        "ok": True,
    }


def test_master_bridge_rejects_legacy_or_partial_results_then_atomically_links_all(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config()
    secure_write_json(paths.config_file, config)
    records = {
        "People": [
            {
                "entity_key": "person_1",
                "field_values": {
                    "person_key": "person_1",
                    "record_type": "人员",
                    "name": "Create Person",
                },
            },
            {
                "entity_key": "review:iss_1",
                "field_values": {
                    "person_key": "review:iss_1",
                    "record_type": "审核项",
                    "name": "Review Person",
                    "review_status": "待补主页",
                },
            },
        ],
        "Sources": [],
    }
    state = PortableState(paths.database)
    try:
        state.set_feishu_record_link(
            entity_kind="person",
            entity_key="review:iss_1",
            base_token="bas_master",
            table_id="tbl_people",
            record_id="rec_review",
        )
        records = cli_module._bridge_safe_master_records(
            state,
            records,
            authoritative_roster=False,
            base_token="bas_master",
            people_table_id="tbl_people",
            live_records={
                "People": [
                    {
                        "record_id": "rec_review",
                        "fields": {
                            "Name": "Review Person",
                            "Person Key": "review:iss_1",
                            "Record Type": "审核项",
                            "Review Status": "旧状态",
                            "Human Notes": "preserve",
                        },
                    }
                ]
            },
        )
    finally:
        state.close()

    request = _new_bridge_request(
        paths,
        purpose="master_sync",
        expected_refs=_master_expected_refs(records, include_sources=False),
        request_content={"operation": "test", "records": records},
    )

    legacy = tmp_path / "legacy.json"
    legacy_payload = _master_result_payload(request, outcomes=[])
    legacy_payload.pop("schema_fields")
    legacy_payload.pop("outcomes")
    secure_write_json(legacy, legacy_payload)
    with pytest.raises(ValueError, match="schema_fields"):
        command_sync(_sync_args(master_bridge_results=legacy), paths)

    partial = tmp_path / "partial.json"
    secure_write_json(
        partial,
        _master_result_payload(
            request,
            outcomes=[_verified_outcome(request, "person_1", "rec_person")],
        ),
    )
    with pytest.raises(ValueError, match="outcome coverage mismatch"):
        command_sync(_sync_args(master_bridge_results=partial), paths)

    wrong_target = tmp_path / "wrong-target.json"
    secure_write_json(
        wrong_target,
        _master_result_payload(
            request,
            outcomes=[
                _verified_outcome(request, "person_1", "rec_person"),
                _verified_outcome(
                    request, "review:iss_1", "rec_duplicate_instead"
                ),
            ],
        ),
    )
    with pytest.raises(ValueError, match="target record_id"):
        command_sync(_sync_args(master_bridge_results=wrong_target), paths)

    human_mutation = tmp_path / "human-mutation.json"
    mutated = _verified_outcome(request, "person_1", "rec_person")
    mutated["human_patch"] = {"name": "Overwritten"}
    secure_write_json(
        human_mutation,
        _master_result_payload(
            request,
            outcomes=[
                mutated,
                _verified_outcome(request, "review:iss_1", "rec_review"),
            ],
        ),
    )
    with pytest.raises(ValueError, match="human-owned"):
        command_sync(_sync_args(master_bridge_results=human_mutation), paths)

    post_read_mutation = tmp_path / "post-read-mutation.json"
    mutated_readback = _verified_outcome(request, "review:iss_1", "rec_review")
    # Mutate the real full-row readback while keeping human_patch empty; the
    # verifier must derive the unauthorized field difference itself.
    mutated_readback["post_read"]["fields"]["Human Notes"] = "overwritten"
    mutated_readback["post_read_hash"] = cli_module._bridge_contract_hash(
        mutated_readback["post_read"]
    )
    secure_write_json(
        post_read_mutation,
        _master_result_payload(
            request,
            outcomes=[
                _verified_outcome(request, "person_1", "rec_person"),
                mutated_readback,
            ],
        ),
    )
    with pytest.raises(ValueError, match="human-owned"):
        command_sync(_sync_args(master_bridge_results=post_read_mutation), paths)

    state = PortableState(paths.database)
    try:
        assert state.get_meta("bridge_request:master_sync")["status"] == "pending"
        links_before = {
            row["entity_key"]: row["record_id"]
            for row in state.db.execute(
                "SELECT entity_key,record_id FROM feishu_record_links"
            )
        }
        assert links_before == {"review:iss_1": "rec_review"}
    finally:
        state.close()

    complete = tmp_path / "complete.json"
    secure_write_json(
        complete,
        _master_result_payload(
            request,
            outcomes=[
                _verified_outcome(request, "person_1", "rec_person"),
                _verified_outcome(request, "review:iss_1", "rec_review"),
            ],
        ),
    )
    applied = command_sync(_sync_args(master_bridge_results=complete), paths)
    assert applied["linked_records"] == 2
    state = PortableState(paths.database)
    try:
        assert state.get_meta("bridge_request:master_sync")["status"] == "consumed"
        links = {
            row["entity_key"]: row["record_id"]
            for row in state.db.execute(
                "SELECT entity_key,record_id FROM feishu_record_links"
            )
        }
        assert links == {"person_1": "rec_person", "review:iss_1": "rec_review"}
    finally:
        state.close()


def test_openclaw_schedule_is_an_isolated_host_agent_turn(tmp_path: Path) -> None:
    launcher = tmp_path / "bin" / "people-tracking-feishu"
    plan = openclaw_cron_plan(launcher, "Asia/Shanghai")[0]
    argv = plan["create_argv"]

    assert argv[:3] == ["openclaw", "automations", "add"]
    assert argv[argv.index("--cron") + 1] == "*/15 * * * *"
    assert argv[argv.index("--tz") + 1] == "Asia/Shanghai"
    assert argv[argv.index("--session") + 1] == "isolated"
    assert "--no-deliver" in argv and "--json" in argv
    assert "--command-argv" not in argv
    message = argv[argv.index("--message") + 1]
    assert "$people-tracking" in message
    assert str(launcher) in message
    assert "自己的 token" in message
    assert "source bridge" in message and "master bridge" in message
    assert "execution_agent_review" in message and "delivery bridge" in message
    assert "不得发送 public" in message


def test_local_schedule_tick_fails_closed_without_a_host_agent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    secure_write_json(paths.config_file, _enabled_config(runtime="claude_lark_cli"))

    with pytest.raises(RuntimeError, match="isolated OpenClaw host-Agent turn"):
        cli_module._install_configured_scheduler(
            _enabled_config(runtime="claude_lark_cli"),
            argparse.Namespace(launcher=tmp_path / "launcher"),
        )
    result = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)

    assert result["agent_turn_required"] is True
    assert result["public_delivery_blocked"] is True
    state = PortableState(paths.database)
    try:
        run = state.db.execute(
            "SELECT status,metrics_json FROM runtime_runs "
            "WHERE action='scheduler_agent_required'"
        ).fetchone()
        assert run["status"] == "failed"
        assert json.loads(run["metrics_json"])["audience"] == "developer"
    finally:
        state.close()


def test_claude_bootstrap_requires_explicit_interactive_only_acknowledgement(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    config = normalize_answers(_existing_base_answers(runtime="claude_lark_cli"))
    secure_write_json(paths.config_file, config)
    mark_state(paths, config, "validated", validation={"all_ok": True})

    result = command_bootstrap(
        argparse.Namespace(
            confirmation="确认启用",
            skip_schedule=False,
            lark_cli=None,
            launcher=None,
            source_bridge_input=None,
            anchor_bridge_input=None,
            master_bridge_results=None,
        ),
        paths,
    )

    assert result["status"] == "waiting_for_interactive_only_confirmation"
    assert result["scheduler"]["status"] == "unsupported"
    assert result["public_delivery_blocked"] is True
    assert "--skip-schedule" in result["next"]
    assert not paths.database.exists()


def test_openclaw_scheduler_resumes_checkpoint_and_blocks_public_until_master_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    config = _enabled_config()
    secure_write_json(paths.config_file, config)

    first = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)
    assert first["roster_bridge_waiting"] is True
    bridge = next(
        item["result"]
        for item in first["actions"]
        if item["operation"] == "reconcile_roster" and "result" in item
    )
    request = bridge["bridge_request"]
    source_ref = config["master_database"]["authoritative_source_ref"]
    source_file = tmp_path / "source-result.json"
    secure_write_json(
        source_file,
        {
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
                                "record_id": "rec_scheduler",
                                "fields": {
                                    "Name": "Scheduled Person",
                                    "ID": "scheduled-1",
                                    "Homepage": "https://example.org/scheduled",
                                    "Record Type": "人员",
                                },
                            }
                        ]
                    },
                }
            ],
        },
    )
    consumed = command_sync(_sync_args(bridge_input=source_file), paths)
    assert consumed["apply"] is True
    state = PortableState(paths.database)
    try:
        checkpoint = state.get_meta("scheduler_roster_checkpoint")
        bridge_request = state.get_meta("bridge_request:source_sync")
        assert checkpoint["status"] == "ready"
        assert checkpoint["authoritative_import"]["source_ref"] == source_ref
        changed_mapping = json.loads(json.dumps(config))
        changed_mapping["field_mapping"]["name"] = "Renamed Person"
        assert cli_module._usable_scheduler_roster_checkpoint(
            checkpoint,
            config=changed_mapping,
            now=datetime.now(timezone.utc),
            last_scan=None,
            bridge_request=bridge_request,
        ) is None
        outputs_only = json.loads(json.dumps(config))
        outputs_only["outputs"]["public"]["message"]["enabled"] = True
        assert cli_module._usable_scheduler_roster_checkpoint(
            checkpoint,
            config=outputs_only,
            now=datetime.now(timezone.utc),
            last_scan=None,
            bridge_request=bridge_request,
        ) is not None
    finally:
        state.close()

    monkeypatch.setattr(
        cli_module,
        "scan_due",
        lambda *args, **kwargs: {"validation": {"passed": True}},
    )
    monkeypatch.setattr(cli_module, "_digest_due_now", lambda kind, *args: kind == "daily")
    public_calls: list[str] = []
    developer_calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "deliver_digest",
        lambda *args, **kwargs: public_calls.append(kwargs["output_kind"])
        or {"delivery": {"status": "completed"}},
    )
    monkeypatch.setattr(
        cli_module,
        "deliver_developer_digest",
        lambda *args, **kwargs: developer_calls.append(kwargs["output_kind"])
        or {"delivery": {"status": "completed"}},
    )
    monkeypatch.setattr(
        cli_module,
        "export_agent_review_requests",
        lambda *args, **kwargs: {"requests": []},
    )

    second = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)
    assert second["master_bridge_waiting"] is True
    assert second["public_delivery_blocked"] is True
    assert public_calls == []
    assert developer_calls == ["daily"]
    assert any(
        item.get("status") == "reused_fresh_checkpoint"
        for item in second["actions"]
        if item["operation"] == "reconcile_roster"
    )
    assert any(item["operation"] == "scan_due_only" for item in second["actions"])

    state = PortableState(paths.database)
    try:
        master_request = state.get_meta("bridge_request:master_sync")
    finally:
        state.close()
    master_file = tmp_path / "master-result.json"
    outcomes = [
        _verified_outcome(
            master_request,
            ref.split(":", 1)[1],
            contract.get("target_record_id") or "rec_created",
            table=ref.split(":", 1)[0],
        )
        for ref, contract in master_request["expected_writes"].items()
    ]
    secure_write_json(
        master_file,
        _master_result_payload(master_request, outcomes=outcomes),
    )
    command_sync(_sync_args(master_bridge_results=master_file), paths)

    third = command_schedule_tick(argparse.Namespace(lark_cli=None), paths)
    assert third["scan_due"] is False
    assert third["master_bridge_waiting"] is False
    assert public_calls == ["daily"]
    assert developer_calls == ["daily", "daily"]
