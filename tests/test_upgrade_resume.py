from __future__ import annotations

import argparse
import copy
from contextlib import closing
import json
import subprocess

import pytest

from people_tracking_feishu import cli, scheduler
from people_tracking_feishu.config import secure_write_json
from people_tracking_feishu.state import PortableState
from test_feishu_portable import _enabled_scan_config


def test_existing_job_only_refreshes_message_and_readback_is_idempotent(tmp_path, monkeypatch):
    job = {
        "id": "existing-job-id", "name": "people-tracking-feishu-scheduler", "enabled": True,
        "agentId": "existing-agent", "sessionTarget": "isolated",
        "schedule": {"kind": "cron", "expr": "7,22,37,52 * * * *", "tz": "Asia/Shanghai"},
        "delivery": {"mode": "none"}, "payload": {
            "kind": "agentTurn", "message": "Use the installed tracking skill. Keep the operator's custom note.",
            "model": "existing-model",
        },
    }
    original = copy.deepcopy(job)
    commands = []

    def run(argv, **_):
        commands.append(argv)
        if argv[2] == "edit":
            assert argv[3] == job["id"] and argv[4] == "--message" and argv[6:] == ["--json"]
            job["payload"]["message"] = argv[5]
        result = {"jobs": [job]} if argv[2] == "list" else job
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    monkeypatch.setattr(scheduler.shutil, "which", lambda _: "/fixture/openclaw")
    monkeypatch.setattr(scheduler.subprocess, "run", run)
    first = scheduler.refresh_existing_openclaw(tmp_path / "installed-launcher")
    assert first == {"status": "refreshed", "job_id": job["id"], "enabled": True}
    assert job["payload"]["message"].startswith(original["payload"]["message"])
    assert "execution_agent_career_search" in job["payload"]["message"]
    assert "control_debug" in job["payload"]["message"]
    assert {**job, "payload": {**job["payload"], "message": original["payload"]["message"]}} == original
    assert scheduler.refresh_existing_openclaw(tmp_path / "installed-launcher")["status"] == "current"
    assert [command[2] for command in commands] == ["list", "edit", "get", "list"]


@pytest.mark.parametrize("developer_document", [False, True])
def test_upgrade_inherits_existing_outputs_without_a_setup_question(
    tmp_path, monkeypatch, developer_document
):
    paths, config = _enabled_scan_config(tmp_path)
    config["runtime"]["mode"] = "openclaw"
    config["outputs"]["developer"]["message"] = {"enabled": False}
    config["outputs"]["developer"]["document"] = {"enabled": developer_document}
    secure_write_json(paths.config_file, config)
    saved = paths.config_file.read_bytes()
    with closing(PortableState(paths.database)) as state:
        state.set_meta("scheduler_digest_weekly", "retained-week")
    monkeypatch.setattr(cli, "refresh_existing_openclaw", lambda _, **roots: {
        "status": "current", "job_id": "existing-id", "enabled": True,
    })
    result = cli.command_upgrade_resume(argparse.Namespace(launcher=None), paths)
    assert not result["user_configuration_required"]
    assert result["existing_schedule_verified"]
    assert paths.config_file.read_bytes() == saved
    assert "控制主页待接入" in result["markdown"]
    if developer_document:
        assert result["actions"][0]["operation"] == "create_document"
        assert result["actions"][0]["audience"] == "developer"
    else:
        assert not result["actions"]
        assert result["control_debug"]["status"] == "saved_to_existing_developer_reports"
    questionnaire = cli.command_onboarding(
        argparse.Namespace(answers=None, enable=False, show=False), paths
    )
    assert questionnaire["state"] == "enabled"
    assert "questionnaire" not in questionnaire
    with closing(PortableState(paths.database)) as state:
        assert state.get_meta("scheduler_digest_weekly") == "retained-week"
