#!/usr/bin/env python3
"""Validate the portable source tree before tests or release construction."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+-portable\.\d+")
SECRET_PATTERNS = {
    "api_key": re.compile(rb"\bsk-[A-Za-z0-9]{24,}\b"),
    "bearer": re.compile(rb"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
LOCAL_PATH = re.compile(rb"/(?:Users|home)/[A-Za-z0-9._-]+/")
FORBIDDEN_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".csv", ".tsv", ".jsonl", ".pem", ".key"}
SKIP_PARTS = {".git", ".ruff_cache", ".venv", "__pycache__", "dist", "build"}
MAX_AUDITED_FILE_BYTES = 8 * 1024 * 1024


def audit_files(root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative_path.parts):
            continue
        if path.is_symlink():
            failures.append(f"symlink:{relative_path.as_posix()}")
            continue
        if not path.is_file():
            continue
        relative = relative_path.as_posix()
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            failures.append(f"forbidden:{relative}")
            continue
        size = path.stat().st_size
        if size > MAX_AUDITED_FILE_BYTES:
            failures.append(f"oversized:{relative}:{size}>{MAX_AUDITED_FILE_BYTES}")
            continue
        data = path.read_bytes()
        if LOCAL_PATH.search(data):
            failures.append(f"local_path:{relative}")
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                failures.append(f"secret:{relative}:{name}")
    return failures


def main() -> int:
    failures: list[str] = []
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        failures.append("invalid VERSION")

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected_python_version = version.split("-portable.", 1)[0]
    if pyproject.get("project", {}).get("version") != expected_python_version:
        failures.append("pyproject version does not match VERSION base")
    scripts = pyproject.get("project", {}).get("scripts", {})
    if scripts.get("people-tracking-feishu") != "people_tracking_feishu.cli:main":
        failures.append("people-tracking-feishu console entry is missing")

    skill = ROOT / "packages/feishu/people-tracking/SKILL.md"
    text = skill.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\nname: people-tracking\n" not in text:
        failures.append("invalid Skill frontmatter")
    if "homepage-reliability.md" not in text or "--force-full-fetch" not in text:
        failures.append("Skill reliability instructions are missing")

    agent_yaml = (skill.parent / "agents/openai.yaml").read_text(encoding="utf-8")
    if "$people-tracking" not in agent_yaml:
        failures.append("openai.yaml default_prompt must mention $people-tracking")

    handoff = json.loads((ROOT / "deploy/feishu/agent-handoff.json").read_text(encoding="utf-8"))
    if handoff.get("release") != "@@VERSION@@":
        failures.append("agent handoff must use the build-time version placeholder")

    failures.extend(audit_files(ROOT))

    print(json.dumps({"ok": not failures, "version": version, "failures": failures}, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
