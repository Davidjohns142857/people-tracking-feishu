from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from people_tracking_feishu.config import (
    ConfigError,
    RuntimePaths,
    normalize_answers,
    mark_state,
    secure_write_json,
    validate_config,
)
from people_tracking_feishu.cli import command_bootstrap, command_sync, parser
from people_tracking_feishu.ingest import canonical_records, records_from_file
from people_tracking_feishu.lark import LarkCli
from people_tracking_feishu.master import create_master_base, sync_visible_master
from people_tracking_feishu.sandbox import algorithm_e2e, feishu_e2e
from people_tracking_feishu.scheduler import scheduler_artifacts
from people_tracking_feishu.state import PortableState
from people_tracking_feishu.tracking import deliver_digest


ROOT = Path(__file__).parents[1]
RELEASE_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def answers(source_path: Path) -> dict:
    return {
        "runtime": {"mode": "claude_lark_cli"},
        "sources": [{"kind": "local_file", "path": str(source_path)}],
        "field_mapping": {
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
        "master_database": {"mode": "local_or_api"},
        "outputs": {
            "document": {"enabled": True},
            "message": {"enabled": True, "target_kind": "chat", "target_id": "oc_test"},
        },
        "schedule": {
            "timezone": "Asia/Shanghai",
            "scan": "weekly",
            "daily_digest": "18:00",
            "weekly_digest": "MON 08:30",
        },
        "apis": {"deepseek": {"enabled": False, "model": "deepseek-v4-flash"}},
    }


def fake_lark_cli(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "lark-calls.jsonl"
    script = tmp_path / "lark-cli"
    script.write_text(
        """#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
log=Path(os.environ['FAKE_LARK_LOG'])
with log.open('a',encoding='utf-8') as h: h.write(json.dumps(args,ensure_ascii=False)+'\\n')
if args == ['--version']:
    print('lark-cli version 1.0.82'); raise SystemExit(0)
if args[:2] == ['auth','status']:
    print(json.dumps({'identity':'bot','brand':'feishu','identities':{'bot':{'available':True},'user':{'available':True}}})); raise SystemExit(0)
if args and args[0] == 'doctor':
    print(json.dumps({'ok':True,'checks':[]})); raise SystemExit(0)
dry='--dry-run' in args
data={}
if '+base-create' in args: data={'base_token':'bas_test'}
elif '+table-create' in args:
    name=args[args.index('--name')+1]; data={'table_id':'tbl_people' if name=='People' else 'tbl_sources'}
elif '+record-upsert' in args: data={'record_id':'rec_test'}
elif '+record-list' in args:
    offset=int(args[args.index('--offset')+1])
    if offset == 0: data={'items':[{'record_id':'rec_1','fields':{'Name':'Synthetic'}}],'has_more':True,'next_offset':1}
    else: data={'items':[{'record_id':'rec_2','fields':{'Name':'Synthetic 2'}}],'has_more':False}
elif '+create' in args and args[0]=='docs': data={'url':'https://example.feishu.cn/docx/doc_test','document_id':'doc_test'}
elif '+fetch' in args and args[0]=='docs': data={'title':'[TEST] report','markdown':'# report'}
elif '+messages-send' in args: data={'message_id':'om_test'}
elif '+base-get' in args: data={'name':'Synthetic Base'}
elif '+table-list' in args: data={'items':[{'table_id':'tbl_people','name':'People'}]}
elif '+field-list' in args: data={'items':[{'field_id':'fld_name','field_name':'Name'}]}
elif '+node-get' in args: data={'obj_type':'bitable','obj_token':'bas_test'}
print(json.dumps({'ok':True,'dry_run':dry,'data':data},ensure_ascii=False))
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, log


def test_config_rejects_inline_secret(tmp_path: Path):
    payload = normalize_answers(answers(tmp_path / "people.md"))
    payload["apis"]["deepseek"].update(
        {"enabled": True, "key_reference": "sk-" + "123456789012345678901234"}
    )
    with pytest.raises(ConfigError, match="inline secret"):
        validate_config(payload)


def test_markdown_sync_keeps_missing_anchor_in_review(tmp_path: Path):
    source = tmp_path / "people.md"
    source.write_text(
        """| Name | ID | Aliases | School | Research | Stage | Homepage | Scholar | GitHub | LinkedIn |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Synthetic Person | p1 | 合成人物 | Synthetic U | ML | PhD | https://synthetic.invalid/person | | | |
| Missing Anchor | p2 | | Synthetic U | Systems | PhD | | | | |
""",
        encoding="utf-8",
    )
    records = canonical_records(records_from_file(source), answers(source)["field_mapping"])
    state = PortableState(tmp_path / "state.sqlite3")
    try:
        report = state.import_people(records, source_ref="source-1")
        assert report["counts"] == {
            "input": 2,
            "admitted": 1,
            "updated": 0,
            "needs_anchor": 1,
            "invalid": 0,
        }
        assert state.status()["counts"]["people"] == 1
        assert state.status()["counts"]["open_intake_issues"] == 1
    finally:
        state.close()


def test_algorithm_sandbox_confirms_two_observations(tmp_path: Path):
    result = algorithm_e2e(tmp_path / "sandbox.sqlite3")
    assert result["decisions"] == ["baseline", "candidate", "changed"]
    assert result["passed"] is True


def test_fake_feishu_sandbox_writes_and_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    executable, log = fake_lark_cli(tmp_path)
    monkeypatch.setenv("FAKE_LARK_LOG", str(log))
    algorithm = algorithm_e2e(tmp_path / "algorithm.sqlite3")
    result = feishu_e2e(
        lark=LarkCli(executable),
        algorithm=algorithm,
        apply=True,
        message_target={"target_kind": "chat", "target_id": "oc_test"},
    )
    assert result["readback_passed"] is True
    assert result["objects"]["document_url"].endswith("doc_test")
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    writes = [call for call in calls if any(name in call for name in ("+base-create", "+table-create", "+record-upsert", "+create", "+messages-send"))]
    for index in range(0, len(writes), 2):
        assert "--dry-run" in writes[index]
        assert "--dry-run" not in writes[index + 1]


def test_lark_cli_enforces_dry_run_and_pagination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    executable, log = fake_lark_cli(tmp_path)
    monkeypatch.setenv("FAKE_LARK_LOG", str(log))
    lark = LarkCli(executable)
    lark.assert_pinned()
    preview, actual = lark.send_message(
        markdown="synthetic; $(not-a-shell)",
        idempotency_key="idempotent-test",
        target_kind="chat",
        target_id="oc_test;touch_should_not_run",
        apply=True,
    )
    assert preview.dry_run is True
    assert actual and actual.payload["ok"] is True
    records = lark.list_base_records(base_token="bas_test", table_id="tbl_people")
    assert [row["record_id"] for row in records] == ["rec_1", "rec_2"]
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    sends = [call for call in calls if "+messages-send" in call]
    assert len(sends) == 2
    assert "--dry-run" in sends[0] and "--dry-run" not in sends[1]
    assert sends[1][sends[1].index("--chat-id") + 1] == "oc_test;touch_should_not_run"
    assert not (tmp_path / "touch_should_not_run").exists()


def test_digest_is_idempotent_after_document_and_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    executable, log = fake_lark_cli(tmp_path)
    monkeypatch.setenv("FAKE_LARK_LOG", str(log))
    source = tmp_path / "people.md"
    source.write_text("synthetic", encoding="utf-8")
    config = normalize_answers(answers(source))
    config["state"] = "enabled"
    config["validation"] = {"all_ok": True}
    validate_config(config, require_complete=True)
    paths = RuntimePaths(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        config_file=tmp_path / "config/config.json",
        database=tmp_path / "state/people.sqlite3",
        reports=tmp_path / "state/reports",
        install_state=tmp_path / "config/install-state.json",
    )
    paths.ensure()
    state = PortableState(paths.database)
    try:
        person = state.tracker.add_person("Synthetic Person", urls=["https://synthetic.invalid/person"])
        source_id = person["sources"][0]["source_id"]
        from people_intel.light_tracker import FetchObservation

        run_id = state.tracker.start_run("digest-test")
        state.tracker.observe(run_id, source_id, FetchObservation(body="<html><h1>Synthetic Person</h1><p>Research profile</p></html>", status_code=200))
        state.tracker.complete_run(run_id)
        lark = LarkCli(executable)
        first = deliver_digest(state, paths, config, output_kind="daily", tracker_run_id=run_id, apply=True, lark=lark)
        second = deliver_digest(state, paths, config, output_kind="daily", tracker_run_id=run_id, apply=True, lark=lark)
        assert first["delivery"]["status"] == "completed"
        assert second["idempotent_replay"] is True
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert sum("+create" in call and call[0] == "docs" for call in calls) == 2
        assert sum("+messages-send" in call for call in calls) == 2
    finally:
        state.close()


def test_two_table_master_base_is_created_and_synced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    executable, log = fake_lark_cli(tmp_path)
    monkeypatch.setenv("FAKE_LARK_LOG", str(log))
    lark = LarkCli(executable)
    created = create_master_base(
        lark,
        timezone_name="Asia/Shanghai",
        folder_token=None,
        apply=True,
    )
    assert created["table_ids"] == {"People": "tbl_people", "Sources": "tbl_sources"}
    state = PortableState(tmp_path / "master.sqlite3")
    try:
        state.tracker.add_person(
            "Synthetic Master Person",
            urls=["https://synthetic.invalid/master-person"],
            secondary_id=("synthetic", "master-1"),
        )
        result = sync_visible_master(
            state,
            lark,
            base_token=created["base_token"],
            people_table_id=created["table_ids"]["People"],
            sources_table_id=created["table_ids"]["Sources"],
            apply=True,
        )
        assert result["record_counts"] == {"People": 1, "Sources": 1}
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert sum("+record-upsert" in call for call in calls) == 4
    finally:
        state.close()


def test_scheduler_artifacts_are_namespaced(tmp_path: Path):
    mac = scheduler_artifacts(home=tmp_path, launcher=tmp_path / "bin/ptf", platform="darwin")
    assert all("people-tracking-feishu-" in path.name for path in mac)
    linux = scheduler_artifacts(home=tmp_path, launcher=tmp_path / "bin/ptf", platform="linux")
    assert {path.suffix for path in linux} == {".service", ".timer"}
    assert all("people-tracking-feishu-" in path.name for path in linux)
    assert b"schedule-tick" in next(content for path, content in linux.items() if path.suffix == ".service")


def _bootstrap_paths(tmp_path: Path) -> RuntimePaths:
    paths = RuntimePaths(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        config_file=tmp_path / "config/config.json",
        database=tmp_path / "state/people.sqlite3",
        reports=tmp_path / "state/reports",
        install_state=tmp_path / "config/install-state.json",
    )
    paths.ensure()
    return paths


def _bootstrap_args(**overrides: object) -> argparse.Namespace:
    values = {
        "lark_cli": None,
        "confirmation": "确认启用",
        "source_bridge_input": None,
        "anchor_bridge_input": None,
        "master_bridge_results": None,
        "launcher": None,
        "skip_schedule": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_bootstrap_runs_import_baseline_and_is_resume_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from people_intel.light_tracker import FetchObservation
    import people_tracking_feishu.tracking as tracking_module

    source = tmp_path / "people.md"
    source.write_text(
        """| Name | ID | Homepage |
| --- | --- | --- |
| Synthetic Active | active-1 | https://synthetic.invalid/active |
| Synthetic Pending | pending-1 | |
""",
        encoding="utf-8",
    )
    payload = answers(source)
    payload["runtime"] = {"mode": "openclaw"}
    payload["intake"] = {"missing_anchor_policy": "manual_queue"}
    config = normalize_answers(payload)
    paths = _bootstrap_paths(tmp_path)
    secure_write_json(paths.config_file, config)
    mark_state(paths, config, "validated", validation={"all_ok": True})
    fetched: list[dict] = []

    def fake_fetch(source_row: dict) -> FetchObservation:
        fetched.append(source_row)
        return FetchObservation(
            body=(
                "<html><h1>Synthetic Active</h1>"
                "<p>Research profile with publications and active projects</p></html>"
            ),
            status_code=200,
        )

    monkeypatch.setattr(tracking_module, "_fetch", fake_fetch)
    first = command_bootstrap(_bootstrap_args(), paths)
    second = command_bootstrap(_bootstrap_args(), paths)
    assert first["status"] == second["status"] == "ready"
    assert first["database"]["counts"]["people"] == 1
    assert first["pending_manual_intake"] == 1
    assert first["baseline"]["metrics"]["decisions"] == {"baseline": 1}
    assert first["baseline"]["validation"]["passed"] is True
    assert first["baseline"]["metrics"]["force_full_fetch"] is True
    assert first["baseline"]["metrics"]["full_fetch_attempted"] == 1
    assert len(fetched) == 1 and fetched[0]["_force_full_fetch"] is True
    assert second["baseline"]["tracker_run_id"] == first["baseline"]["tracker_run_id"]


def test_bootstrap_resumes_agent_anchor_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from people_intel.light_tracker import FetchObservation
    import people_tracking_feishu.tracking as tracking_module

    source = tmp_path / "missing.md"
    source.write_text(
        """| Name | ID | School | Research | Homepage |
| --- | --- | --- | --- | --- |
| Synthetic Missing | missing-1 | Synthetic U | ML | |
""",
        encoding="utf-8",
    )
    payload = answers(source)
    payload["runtime"] = {"mode": "openclaw"}
    payload["intake"] = {"missing_anchor_policy": "agent_discovery", "discovery_batch_size": 20}
    config = normalize_answers(payload)
    paths = _bootstrap_paths(tmp_path)
    secure_write_json(paths.config_file, config)
    mark_state(paths, config, "validated", validation={"all_ok": True})
    waiting = command_bootstrap(_bootstrap_args(), paths)
    assert waiting["status"] == "waiting_for_anchor_discovery"
    request = json.loads(Path(waiting["actions"][0]["payload_file"]).read_text(encoding="utf-8"))
    issue = request["people"][0]
    resolution = tmp_path / "anchor-result.json"
    secure_write_json(
        resolution,
        {
            "all_reviewed": True,
            "resolutions": [
                {
                    "issue_id": issue["issue_id"],
                    "canonical_name": "Synthetic Missing",
                    "urls": ["https://synthetic.invalid/missing"],
                    "evidence_urls": ["https://synthetic.invalid/missing"],
                    "evidence_notes": "name, school and research direction match",
                }
            ],
            "unresolved": [],
        },
    )
    monkeypatch.setattr(
        tracking_module,
        "_fetch",
        lambda source_row: FetchObservation(
            body="<html><h1>Synthetic Missing</h1><p>Synthetic U ML</p></html>",
            status_code=200,
        ),
    )
    ready = command_bootstrap(_bootstrap_args(anchor_bridge_input=resolution), paths)
    assert ready["status"] == "ready"
    assert ready["database"]["counts"]["people"] == 1
    assert ready["pending_manual_intake"] == 0
    assert ready["baseline"]["metrics"]["decisions"] == {"baseline": 1}


def test_bootstrap_resumes_source_and_master_feishu_bridges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from people_intel.light_tracker import FetchObservation
    import people_tracking_feishu.tracking as tracking_module

    payload = answers(tmp_path / "unused.md")
    payload["runtime"] = {"mode": "openclaw"}
    payload["sources"] = [
        {"kind": "feishu_doc", "url": "https://synthetic.feishu.cn/docx/doc_people"}
    ]
    payload["intake"] = {"missing_anchor_policy": "manual_queue"}
    payload["master_database"] = {"mode": "create_base"}
    config = normalize_answers(payload)
    paths = _bootstrap_paths(tmp_path)
    secure_write_json(paths.config_file, config)
    mark_state(paths, config, "validated", validation={"all_ok": True})

    source_wait = command_bootstrap(_bootstrap_args(), paths)
    assert source_wait["status"] == "waiting_for_source_bridge"
    source_result = tmp_path / "source-result.json"
    secure_write_json(
        source_result,
        {
            "sources": [
                {
                    "source_ref": "source-1",
                    "payload": {
                        "records": [
                            {
                                "record_id": "rec_synthetic",
                                "fields": {
                                    "Name": "Synthetic Bridge",
                                    "ID": "bridge-1",
                                    "Homepage": "https://synthetic.invalid/bridge",
                                },
                            }
                        ]
                    },
                }
            ]
        },
    )
    master_wait = command_bootstrap(
        _bootstrap_args(source_bridge_input=source_result),
        paths,
    )
    assert master_wait["status"] == "waiting_for_master_bridge"
    master_result = tmp_path / "master-result.json"
    secure_write_json(
        master_result,
        {
            "all_ok": True,
            "base_token": "bas_synthetic",
            "base_url": "https://synthetic.feishu.cn/base/bas_synthetic",
            "table_ids": {"People": "tbl_people", "Sources": "tbl_sources"},
        },
    )
    monkeypatch.setattr(
        tracking_module,
        "_fetch",
        lambda source_row: FetchObservation(
            body=(
                "<html><title>Synthetic Bridge</title><h1>Synthetic Bridge</h1>"
                "<h2>Publications</h2><ul>"
                "<li>Reliable Synthetic Research Systems, 2026</li>"
                "</ul></html>"
            ),
            status_code=200,
        ),
    )
    ready = command_bootstrap(
        _bootstrap_args(master_bridge_results=master_result),
        paths,
    )
    assert ready["status"] == "ready"
    assert ready["database"]["counts"] == {
        "people": 1,
        "sources": 1,
        "open_intake_issues": 0,
        "open_human_conflicts": 0,
        "completed_deliveries": 0,
    }


def test_release_builder_and_verifier(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_feishu_release.py"), "--output-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    archive = Path(payload["archive"])
    assert archive.name == f"people-tracking-feishu-{RELEASE_VERSION}.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == payload["sha256"]
    with zipfile.ZipFile(archive) as handle:
        names = handle.namelist()
        assert any(name.endswith("/release-manifest.json") for name in names)
        assert any(name.endswith("/INSTALL_PROMPT.md") for name in names)
        assert any(name.endswith("/runtime/people_tracking_feishu/cli.py") for name in names)
        assert any(name.endswith("/vendor/wheels/pypinyin-0.55.0-py2.py3-none-any.whl") for name in names)
        assert not any("/artifacts/" in name or "/inputs/" in name for name in names)


def test_install_dry_run_does_not_modify_home(tmp_path: Path):
    build = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_feishu_release.py"), "--output-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    archive = Path(json.loads(build.stdout)["archive"])
    extract = tmp_path / "extract"
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(extract)
    release = extract / f"people-tracking-feishu-{RELEASE_VERSION}"
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    sentinel = home / ".claude" / "settings.json"
    sentinel.write_text('{"keep":true}\n', encoding="utf-8")
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    result = subprocess.run(
        [sys.executable, str(release / "install_bundle.py"), "--dry-run", "--home", str(home), "--runtime-mode", "claude_lark_cli"],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["mutated"] is False
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before
    assert not (home / ".config" / "people-tracking-feishu").exists()
    refused = subprocess.run(
        [sys.executable, str(release / "install_bundle.py"), "--apply", "--home", str(home), "--runtime-mode", "claude_lark_cli"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 2
    assert not (home / ".config" / "people-tracking-feishu").exists()
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before


def test_install_apply_repeat_and_rollback_preserve_existing_files(tmp_path: Path):
    build = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_feishu_release.py"), "--output-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    archive = Path(json.loads(build.stdout)["archive"])
    extract = tmp_path / "extract-apply"
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(extract)
    release = extract / f"people-tracking-feishu-{RELEASE_VERSION}"
    home = tmp_path / "home-apply"
    skills = home / ".claude" / "skills"
    for name in ("lark-shared", "lark-doc", "lark-base", "lark-im"):
        path = skills / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: official test skill\n---\n", encoding="utf-8")
    existing_skill = skills / "people-tracking"
    existing_skill.mkdir()
    (existing_skill / "SKILL.md").write_text("old-skill\n", encoding="utf-8")
    settings = home / ".claude" / "settings.json"
    settings.write_text('{"keep":true}\n', encoding="utf-8")
    settings_hash = hashlib.sha256(settings.read_bytes()).hexdigest()
    launcher = home / ".local" / "bin" / "people-tracking-feishu"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho old-launcher\n", encoding="utf-8")
    launcher.chmod(0o700)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_lark, _ = fake_lark_cli(fakebin)
    node = fakebin / "node"
    node.write_text("#!/bin/sh\necho v24.1.0\n", encoding="utf-8")
    node.chmod(0o755)
    environment = {**os.environ, "PATH": f"{fakebin}:{os.environ.get('PATH','')}", "FAKE_LARK_LOG": str(tmp_path / "install-lark.jsonl")}
    command = [
        sys.executable,
        str(release / "install_bundle.py"),
        "--apply",
        "--home",
        str(home),
        "--runtime-mode",
        "claude_lark_cli",
        "--python",
        sys.executable,
        "--lark-cli",
        str(fake_lark),
    ]
    first = subprocess.run(command, capture_output=True, text=True, env=environment, check=False, timeout=180)
    assert first.returncode == 0, first.stderr
    first_report = json.loads(first.stdout)
    assert first_report["mutated"] is True
    assert (existing_skill / "references" / "tracking-algorithm.md").is_file()
    assert list(existing_skill.parent.glob("people-tracking.backup-*"))
    assert hashlib.sha256(settings.read_bytes()).hexdigest() == settings_hash
    assert (
        home
        / ".local/state/people-tracking-feishu/venvs"
        / RELEASE_VERSION
        / "bin/python"
    ).is_file()
    second = subprocess.run(command, capture_output=True, text=True, env=environment, check=True, timeout=180)
    assert json.loads(second.stdout)["result"]["state"]["skills"][0]["changed"] is False
    rolled = subprocess.run(
        [sys.executable, str(release / "install_bundle.py"), "--rollback", "--home", str(home)],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    assert json.loads(rolled.stdout)["rolled_back"] is True
    assert (existing_skill / "SKILL.md").read_text(encoding="utf-8") == "old-skill\n"
    assert "old-launcher" in launcher.read_text(encoding="utf-8")
    assert hashlib.sha256(settings.read_bytes()).hexdigest() == settings_hash


def _enabled_scan_config(tmp_path: Path) -> tuple[RuntimePaths, dict]:
    source = tmp_path / "scan-people.md"
    source.write_text("synthetic", encoding="utf-8")
    config = normalize_answers(answers(source))
    config["state"] = "enabled"
    config["validation"] = {"all_ok": True}
    paths = _bootstrap_paths(tmp_path)
    return paths, config


def test_scan_parser_keeps_force_full_fetch_independent_and_filters_kinds():
    from people_tracking_feishu.cli import parser

    defaults = parser().parse_args(["scan"])
    assert defaults.force_all is False
    assert defaults.force_full_fetch is False
    assert defaults.source_kind is None
    assert defaults.max_error_rate == 0.10
    assert defaults.homepage_retries == 1
    assert defaults.homepage_backoff_seconds == 1.0

    body_only = parser().parse_args(
        ["scan", "--force-full-fetch", "--source-kind", "homepage"]
    )
    assert body_only.force_full_fetch is True
    assert body_only.force_all is False
    assert body_only.source_kind == ["homepage"]
    with pytest.raises(SystemExit):
        parser().parse_args(["scan", "--homepage-retries", "2"])


def test_scan_periodically_fetches_full_body_and_reports_retry_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from people_intel.light_tracker import FetchObservation
    import people_tracking_feishu.tracking as tracking_module

    paths, config = _enabled_scan_config(tmp_path)
    state = PortableState(paths.database)
    try:
        person = state.tracker.add_person(
            "Synthetic Retry",
            urls=["https://synthetic.invalid/retry"],
        )
        source_id = person["sources"][0]["source_id"]
        state.db.execute(
            """UPDATE sources SET last_full_fetch_at=?,full_fetch_interval_days=7,
                      etag='synthetic-etag',last_modified='synthetic-date'
               WHERE source_id=?""",
            ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), source_id),
        )
        state.db.commit()
        observations = [
            FetchObservation(body=None, status_code=503, error="temporary upstream"),
            FetchObservation(
                body=(
                    "<html><title>Synthetic Retry</title><h1>Synthetic Retry</h1>"
                    "<h2>Publications</h2><ul><li>Reliable Systems, 2026</li></ul>"
                    "</html>"
                ),
                status_code=200,
            ),
        ]
        fetched: list[dict] = []
        waits: list[float] = []

        def fake_fetch(source_row: dict) -> FetchObservation:
            fetched.append(source_row)
            return observations.pop(0)

        monkeypatch.setattr(tracking_module, "_fetch", fake_fetch)
        monkeypatch.setattr(tracking_module, "sleep", waits.append)
        result = tracking_module.scan_due(
            state,
            paths,
            config,
            source_kinds=["homepage"],
            max_error_rate=1.0,
        )
        metrics = result["metrics"]
        assert len(fetched) == 2
        assert all(row["_force_full_fetch"] is True for row in fetched)
        assert metrics["force_all"] is False
        assert metrics["force_full_fetch"] is False
        assert metrics["sources_planned"] == metrics["sources_attempted"] == 1
        assert metrics["full_fetch_planned"] == metrics["full_fetch_attempted"] == 1
        assert metrics["homepage_planned"] == metrics["homepage_attempted"] == 1
        assert metrics["homepage_retries"] == 1
        assert len(waits) == 1
        assert metrics["homepage_retry_wait_seconds"] == round(waits[0], 6)
    finally:
        state.close()


@pytest.mark.parametrize(
    ("status_code", "error", "body"),
    [
        (403, "forbidden", None),
        (404, "not found", None),
        (410, "gone", None),
        (429, "rate limited", None),
        (0, "certificate verify failed: hostname mismatch", None),
        (503, None, "<html>CAPTCHA challenge</html>"),
        (503, None, "<html>authentication wall; sign in to continue</html>"),
    ],
)
def test_homepage_does_not_retry_permanent_or_interactive_failures(
    status_code: int,
    error: str | None,
    body: str | None,
    monkeypatch: pytest.MonkeyPatch,
):
    from people_intel.light_tracker import FetchObservation
    import people_tracking_feishu.tracking as tracking_module

    calls = 0

    def fake_fetch(source_row: dict) -> FetchObservation:
        nonlocal calls
        calls += 1
        return FetchObservation(body=body, status_code=status_code, error=error)

    monkeypatch.setattr(tracking_module, "_fetch", fake_fetch)
    monkeypatch.setattr(
        tracking_module,
        "sleep",
        lambda _: pytest.fail("non-retryable Homepage failure must not sleep"),
    )
    _, retries, waited = tracking_module._fetch_with_policy(
        {
            "source_id": "src_permanent",
            "kind": "homepage",
            "url": "https://synthetic.invalid/permanent",
        }
    )
    assert calls == 1
    assert retries == 0
    assert waited == 0.0


def test_force_all_excludes_retired_sources_and_honors_source_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from people_intel.light_tracker import FetchObservation
    import people_tracking_feishu.tracking as tracking_module

    paths, config = _enabled_scan_config(tmp_path)
    state = PortableState(paths.database)
    try:
        active = state.tracker.add_person(
            "Synthetic Active Homepage",
            urls=["https://synthetic.invalid/active-homepage"],
        )
        retired = state.tracker.add_person(
            "Synthetic Retired Homepage",
            urls=["https://synthetic.invalid/retired-homepage"],
        )
        state.tracker.add_person(
            "Synthetic GitHub",
            urls=["https://github.com/synthetic-test-user"],
        )
        retired_id = retired["sources"][0]["source_id"]
        state.db.execute(
            "UPDATE sources SET tracking_enabled=0 WHERE source_id=?",
            (retired_id,),
        )
        state.db.commit()
        fetched: list[str] = []

        def fake_fetch(source_row: dict) -> FetchObservation:
            fetched.append(source_row["source_id"])
            return FetchObservation(
                body=(
                    "<html><h1>Synthetic Active Homepage</h1>"
                    "<h2>Research</h2><ul><li>Synthetic result</li></ul></html>"
                ),
                status_code=200,
            )

        monkeypatch.setattr(tracking_module, "_fetch", fake_fetch)
        result = tracking_module.scan_due(
            state,
            paths,
            config,
            force_all=True,
            source_kinds=["homepage"],
            max_error_rate=1.0,
        )
        assert fetched == [active["sources"][0]["source_id"]]
        assert result["metrics"]["source_kinds"] == ["homepage"]
        assert result["metrics"]["sources_planned"] == 1
    finally:
        state.close()


def test_scan_validation_failure_keeps_written_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from people_intel.light_tracker import FetchObservation
    import people_tracking_feishu.tracking as tracking_module

    paths, config = _enabled_scan_config(tmp_path)
    state = PortableState(paths.database)
    try:
        state.tracker.add_person(
            "Synthetic Missing",
            urls=["https://synthetic.invalid/missing"],
        )
        monkeypatch.setattr(
            tracking_module,
            "_fetch",
            lambda source_row: FetchObservation(
                body=None,
                status_code=404,
                error="not found",
            ),
        )
        result = tracking_module.scan_due(
            state,
            paths,
            config,
            source_kinds=["homepage"],
            max_error_rate=0.0,
        )
        assert result["validation"]["passed"] is False
        assert result["validation"]["error_sources"] == 1
        assert "exceeds" in result["validation"]["reasons"][0]
        assert result["reasons"] == result["validation"]["reasons"]
        assert result["next"]
        assert state.get_meta("last_tracker_run_id") is None
        count = state.db.execute(
            "SELECT COUNT(*) AS count FROM observations WHERE run_id=?",
            (result["tracker_run_id"],),
        ).fetchone()["count"]
        assert count == 1
    finally:
        state.close()


def test_bootstrap_does_not_become_ready_when_baseline_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import people_tracking_feishu.cli as cli_module

    source = tmp_path / "bootstrap-failed.md"
    source.write_text(
        """| Name | ID | Homepage |
| --- | --- | --- |
| Synthetic Failed | failed-1 | https://synthetic.invalid/failed |
""",
        encoding="utf-8",
    )
    payload = answers(source)
    payload["runtime"] = {"mode": "openclaw"}
    payload["intake"] = {"missing_anchor_policy": "manual_queue"}
    config = normalize_answers(payload)
    paths = _bootstrap_paths(tmp_path)
    secure_write_json(paths.config_file, config)
    mark_state(paths, config, "validated", validation={"all_ok": True})
    failed_validation = {
        "passed": False,
        "max_error_rate": 0.10,
        "error_sources": 1,
        "observed_sources": 1,
        "error_rate": 1.0,
        "reasons": ["source error rate 100.00% exceeds the 10.00% limit"],
    }
    monkeypatch.setattr(
        cli_module,
        "scan_due",
        lambda *args, **kwargs: {
            "tracker_run_id": "tracker_failed",
            "metrics": {"validation": failed_validation},
            "validation": failed_validation,
        },
    )
    result = command_bootstrap(_bootstrap_args(), paths)
    assert result["status"] == "baseline_validation_failed"
    assert result["baseline"]["validation"]["passed"] is False
    assert "scheduling was not enabled" in result["next"]


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://user:password@example.org/profile",
        "https://127.0.0.1/profile",
        "https://example.org:8443/profile",
        "https://example.org/profile?access_token=secret",
        "file:///tmp/profile",
    ],
)
def test_source_routes_reject_non_public_or_credentialed_urls(
    tmp_path: Path,
    unsafe_url: str,
):
    payload = answers(tmp_path / "people.md")
    payload["source_routes"] = [
        {
            "url": "https://example.org/people/synthetic",
            "preferred_route": "direct",
            "alternate_urls": [unsafe_url],
        }
    ]
    with pytest.raises(ConfigError):
        normalize_answers(payload)


def test_source_routes_preview_apply_and_replay_preserve_evidence(tmp_path: Path):
    payload = answers(tmp_path / "people.md")
    payload["source_routes"] = [
        {
            "url": "https://www.synthetic.invalid/configured",
            "preferred_route": "direct",
            "follow_meta_refresh": True,
            "alternate_routes": [
                {
                    "route": "raw_html",
                    "url": "https://static.synthetic.invalid/configured",
                }
            ],
        },
        {
            "url": "https://synthetic.invalid/retired",
            "action": "soft_retire",
            "curation_note": "The registered page belongs to another person",
        },
        {
            "url": "https://synthetic.invalid/old",
            "action": "replace",
            "replacement_url": "https://www.synthetic.invalid/new",
            "preferred_route": "direct",
            "curation_note": "Verified public profile migration",
        },
    ]
    config = normalize_answers(payload)
    state = PortableState(tmp_path / "routes.sqlite3")
    try:
        configured = state.tracker.add_person(
            "Synthetic Configured",
            urls=["https://www.synthetic.invalid/configured"],
        )
        retired = state.tracker.add_person(
            "Synthetic Retired",
            urls=["https://synthetic.invalid/retired"],
        )
        replaced = state.tracker.add_person(
            "Synthetic Replaced",
            urls=["https://synthetic.invalid/old"],
        )
        source_ids = {
            "configured": configured["sources"][0]["source_id"],
            "retired": retired["sources"][0]["source_id"],
            "replaced": replaced["sources"][0]["source_id"],
        }
        for index, source_id in enumerate(source_ids.values(), start=1):
            state.db.execute(
                """UPDATE sources SET snapshot_json=?,semantic_hash=?,
                          candidate_snapshot_json=?,candidate_hash=?,candidate_count=?
                   WHERE source_id=?""",
                (
                    json.dumps({"baseline": index}),
                    f"baseline-{index}",
                    json.dumps({"candidate": index}),
                    f"candidate-{index}",
                    index,
                    source_id,
                ),
            )
        state.db.commit()
        evidence_before = {
            row["source_id"]: tuple(row)
            for row in state.db.execute(
                """SELECT source_id,snapshot_json,semantic_hash,candidate_snapshot_json,
                          candidate_hash,candidate_count FROM sources
                   WHERE source_id IN (?,?,?) ORDER BY source_id""",
                tuple(source_ids.values()),
            )
        }

        preview = state.sync_source_routes(config["source_routes"], apply=False)
        assert preview["mutated"] is False
        assert {item["status"] for item in preview["routes"]} == {"ready"}
        assert state.db.execute("SELECT COUNT(*) FROM source_curation_audit").fetchone()[0] == 0

        applied = state.sync_source_routes(config["source_routes"], apply=True)
        assert applied["counts"] == {
            "configured": 1,
            "soft_retired": 1,
            "replaced": 1,
            "no_change": 0,
            "blocked": 0,
            "audits_written": 3,
        }
        configured_row = state.db.execute(
            "SELECT retrieval_config_json FROM sources WHERE source_id=?",
            (source_ids["configured"],),
        ).fetchone()
        assert json.loads(configured_row["retrieval_config_json"])["alternate_routes"][0][
            "route"
        ] == "raw_html"
        assert state.db.execute(
            "SELECT tracking_enabled FROM sources WHERE source_id=?",
            (source_ids["retired"],),
        ).fetchone()[0] == 0
        old = state.db.execute(
            "SELECT tracking_enabled,curation_status FROM sources WHERE source_id=?",
            (source_ids["replaced"],),
        ).fetchone()
        assert tuple(old) == (0, "replaced")
        replacement = state.db.execute(
            """SELECT tracking_enabled,curation_status,retrieval_config_json,snapshot_json
               FROM sources WHERE person_key=? AND url=?""",
            (replaced["person_key"], "https://www.synthetic.invalid/new"),
        ).fetchone()
        assert replacement is not None
        assert replacement["tracking_enabled"] == 1
        assert replacement["curation_status"] == "replacement"
        assert json.loads(replacement["retrieval_config_json"])["preferred_route"] == "direct"
        assert replacement["snapshot_json"] is None
        evidence_after = {
            row["source_id"]: tuple(row)
            for row in state.db.execute(
                """SELECT source_id,snapshot_json,semantic_hash,candidate_snapshot_json,
                          candidate_hash,candidate_count FROM sources
                   WHERE source_id IN (?,?,?) ORDER BY source_id""",
                tuple(source_ids.values()),
            )
        }
        assert evidence_after == evidence_before

        replay = state.sync_source_routes(config["source_routes"], apply=True)
        assert replay["counts"]["no_change"] == 3
        assert replay["counts"]["audits_written"] == 0
        assert state.db.execute("SELECT COUNT(*) FROM source_curation_audit").fetchone()[0] == 3
    finally:
        state.close()


def test_sync_apply_imports_then_applies_versioned_source_routes(tmp_path: Path):
    source = tmp_path / "routed-people.md"
    source.write_text(
        """| Name | ID | Homepage |
| --- | --- | --- |
| Synthetic Routed | routed-1 | https://synthetic.invalid/routed |
""",
        encoding="utf-8",
    )
    payload = answers(source)
    payload["runtime"] = {"mode": "openclaw"}
    payload["source_routes"] = [
        {
            "url": "https://synthetic.invalid/routed",
            "preferred_route": "direct",
            "follow_meta_refresh": True,
        }
    ]
    config = normalize_answers(payload)
    config["state"] = "enabled"
    config["validation"] = {"all_ok": True}
    paths = _bootstrap_paths(tmp_path)
    secure_write_json(paths.config_file, config)
    result = command_sync(
        argparse.Namespace(
            lark_cli=None,
            bridge_input=None,
            master_bridge_results=None,
            apply=True,
        ),
        paths,
    )
    assert result["source_routes"]["counts"]["configured"] == 1
    backup_path = Path(result["source_routes"]["backup"]["path"])
    assert backup_path.is_file()
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert json.loads(
            backup.execute("SELECT retrieval_config_json FROM sources").fetchone()[0]
        ) == {}
    finally:
        backup.close()
    state = PortableState(paths.database)
    try:
        retrieval = json.loads(
            state.db.execute("SELECT retrieval_config_json FROM sources").fetchone()[0]
        )
        assert retrieval == {"follow_meta_refresh": True, "preferred_route": "direct"}
        assert state.db.execute("SELECT COUNT(*) FROM source_curation_audit").fetchone()[0] == 1
    finally:
        state.close()


def test_source_routes_cli_is_preview_first():
    preview = parser().parse_args(["source-routes"])
    apply = parser().parse_args(["source-routes", "--apply"])
    assert preview.apply is False
    assert apply.apply is True
