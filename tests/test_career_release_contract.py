from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tomllib

from scripts import build_feishu_release
from people_tracking_feishu.scheduler import openclaw_agent_message, openclaw_cron_plan


ROOT = Path(__file__).resolve().parents[1]


def test_release_stages_audit_scripts_with_no_runtime_dependency(tmp_path):
    staging = tmp_path / ("people-tracking-feishu-" + build_feishu_release.VERSION)
    staging.mkdir()
    build_feishu_release.stage_release(staging)
    files = build_feishu_release.audit(staging)
    metadata = build_feishu_release.manifest(staging, files, 1786579200)
    for name in ("export_coverage_audit.py", "export_discovery_audit.py"):
        relative = "scripts/" + name
        assert relative in files
        assert (staging / relative).read_bytes() == (ROOT / relative).read_bytes()
        result = subprocess.run(
            [sys.executable, "-I", str(staging / relative), "--help"],
            cwd=staging, capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "--database" in result.stdout and "--since" in result.stdout
    assert "scripts/export_coverage_audit.py" in metadata["entrypoints"]["coverage_audit"]
    assert "scripts/export_discovery_audit.py" in metadata["entrypoints"]["discovery_audit"]
    assert not any("artifacts/" in path for path in files)


def test_isolated_host_prompt_consumes_career_action_without_bypassing_review(tmp_path):
    launcher = tmp_path / "people-tracking-feishu"
    message = openclaw_agent_message(launcher)
    assert "execution_agent_career_search" in message
    assert "payload_file" in message
    assert "career-search-apply --input" in message
    assert "标准事实审核候选" in message
    assert "搜索未完成不阻断" in message
    assert "事件日期" in message and "观察时间" in message
    plans = openclaw_cron_plan(launcher, "Asia/Shanghai")
    assert len(plans) == 1
    assert plans[0]["message"] == message
    assert "--no-deliver" in plans[0]["create_argv"]
    assert plans[0]["create_argv"][plans[0]["create_argv"].index("--session") + 1] == "isolated"


def test_release_version_and_handoff_expose_consistent_upgrade_contract():
    version = (ROOT / "VERSION").read_text().strip()
    package = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert version == "0.11.0-portable.2"
    assert package["project"]["version"] == version.split("-portable.")[0]
    readme = (ROOT / "README.md").read_text()
    assert readme.count('release_version="' + version + '"') == 2
    handoff = json.loads((ROOT / "deploy/feishu/agent-handoff.json").read_text())
    assert handoff["release"] == "@@VERSION@@"
    assert handoff["entrypoints"]["career_search_apply"][1] == "career-search-apply"
    assert "career_failure_policy" in handoff["runtime_contract"]
    skill = (ROOT / "packages/feishu/people-tracking/SKILL.md").read_text()
    assert "references/career-discovery.md" in skill
    assert "持续控制主页验证" in skill
