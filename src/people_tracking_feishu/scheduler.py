from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any


JOB_PREFIX = "people-tracking-feishu-"


def scheduler_artifacts(
    *,
    home: Path,
    launcher: Path,
    platform: str,
) -> dict[Path, bytes]:
    if platform == "darwin":
        target = home / "Library" / "LaunchAgents" / f"{JOB_PREFIX}scheduler.plist"
        payload = {
            "Label": f"{JOB_PREFIX}scheduler",
            "ProgramArguments": [str(launcher), "schedule-tick", "--json"],
            "StartInterval": 900,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": str(home / ".local/state/people-tracking-feishu/scheduler.stdout.log"),
            "StandardErrorPath": str(home / ".local/state/people-tracking-feishu/scheduler.stderr.log"),
        }
        return {target: plistlib.dumps(payload, sort_keys=True)}
    if platform == "linux":
        root = home / ".config" / "systemd" / "user"
        service = root / f"{JOB_PREFIX}scheduler.service"
        timer = root / f"{JOB_PREFIX}scheduler.timer"
        return {
            service: (
                "[Unit]\nDescription=People Tracking Feishu scheduler\n\n"
                "[Service]\nType=oneshot\n"
                f"ExecStart={launcher} schedule-tick --json\n"
            ).encode(),
            timer: (
                "[Unit]\nDescription=Run People Tracking Feishu every 15 minutes\n\n"
                "[Timer]\nOnBootSec=5m\nOnUnitActiveSec=15m\nPersistent=true\n\n"
                "[Install]\nWantedBy=timers.target\n"
            ).encode(),
        }
    raise ValueError("scheduler supports macOS and Linux only")


def install_local_scheduler(
    *,
    home: Path,
    launcher: Path,
    platform: str,
    apply: bool,
) -> dict[str, Any]:
    artifacts = scheduler_artifacts(home=home, launcher=launcher, platform=platform)
    collisions = [str(path) for path in artifacts if path.exists()]
    if collisions:
        raise RuntimeError(
            "scheduler collision; refusing to overwrite: " + ", ".join(collisions)
        )
    if not apply:
        return {"apply": False, "files": [str(path) for path in artifacts], "commands": []}
    for path, content in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    commands: list[list[str]] = []
    if platform == "darwin":
        launchctl = shutil.which("launchctl")
        if launchctl:
            commands = [[launchctl, "bootstrap", f"gui/{os.getuid()}", str(next(iter(artifacts)))]]
    else:
        systemctl = shutil.which("systemctl")
        if systemctl:
            commands = [
                [systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now", f"{JOB_PREFIX}scheduler.timer"],
            ]
    completed: list[dict[str, Any]] = []
    for argv in commands:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        completed.append({"argv": argv, "returncode": result.returncode, "stderr": result.stderr[-1000:]})
        if result.returncode != 0:
            raise RuntimeError(f"scheduler registration failed: {argv[0]}")
    return {
        "apply": True,
        "files": [str(path) for path in artifacts],
        "commands": completed,
        "job_prefix": JOB_PREFIX,
    }


def openclaw_cron_plan(launcher: Path, timezone: str) -> list[dict[str, Any]]:
    jobs = [
        {
            "name": f"{JOB_PREFIX}scheduler",
            "schedule": "*/15 * * * *",
            "argv": [str(launcher), "schedule-tick", "--json"],
        }
    ]
    return [
        {
            **job,
            "timezone": timezone,
            "create_argv": [
                "openclaw",
                "cron",
                "create",
                job["schedule"],
                "--tz",
                timezone,
                "--name",
                job["name"],
                "--command-argv",
                json.dumps(job["argv"], ensure_ascii=False),
            ],
        }
        for job in jobs
    ]
