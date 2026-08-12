#!/usr/bin/env python3
"""Build the deterministic portable Feishu people-tracking release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
RELEASE_PREFIX = "people-tracking-feishu"
SOURCE_DATE_EPOCH = 1786579200
PINNED_NPM_INTEGRITY = (
    "sha512-7jqwniqCtiunLPi2vypDu0aHSaPNeG93kRO9UZ9kywU/"
    "XSVSy/PH1L1GJfav1Goi95v3D8TjV5R+ttWnMEvjYQ=="
)
SKIP_NAMES = {"__pycache__", ".DS_Store"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".sqlite3", ".db"}
SECRET_PATTERNS = [
    re.compile(rb"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(rb"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
LOCAL_PATH = re.compile(rb"/(?:Users|home)/[A-Za-z0-9._-]+/")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise RuntimeError(f"symlink rejected: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_tree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if any(part in SKIP_NAMES for part in relative.parts):
            continue
        if path.is_symlink():
            raise RuntimeError(f"symlink rejected: {path}")
        if path.is_dir():
            (destination / relative).mkdir(parents=True, exist_ok=True)
        elif path.suffix.casefold() not in FORBIDDEN_SUFFIXES:
            _copy_file(path, destination / relative)


def stage_release(staging: Path) -> None:
    _copy_file(ROOT / "VERSION", staging / "VERSION")
    _copy_tree(
        ROOT / "packages" / "feishu" / "people-tracking",
        staging / "packages" / "feishu" / "people-tracking",
    )
    _copy_tree(
        ROOT / "src" / "people_tracking_feishu",
        staging / "runtime" / "people_tracking_feishu",
    )
    runtime_people_intel = staging / "runtime" / "people_intel"
    runtime_people_intel.mkdir(parents=True, exist_ok=True)
    (runtime_people_intel / "__init__.py").write_text(
        '"""Minimal portable People Intel tracking runtime."""\n', encoding="utf-8"
    )
    for name in ("light_tracker.py", "light_cli.py", "deepseek_fallback.py"):
        _copy_file(ROOT / "src" / "people_intel" / name, runtime_people_intel / name)
    _copy_tree(ROOT / "src" / "people_intel", staging / "full-source" / "src" / "people_intel")
    for name in ("pyproject.toml", "requirements.openclaw.lock"):
        _copy_file(ROOT / name, staging / "full-source" / name)
    for relative in ("db/schema.sql", "ontology/people-intel-v1.json"):
        _copy_file(ROOT / relative, staging / "full-source" / relative)
    wheel_root = ROOT / "vendor" / "wheels"
    wheels = sorted(wheel_root.glob("pypinyin-0.55.0-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("exactly one pypinyin 0.55.0 wheel is required")
    _copy_file(wheels[0], staging / "vendor" / "wheels" / wheels[0].name)
    deploy = ROOT / "deploy" / "feishu"
    _copy_file(
        deploy / "SOURCE_README.md",
        staging / "full-source" / "README.md",
    )
    for name in (
        "verify_release.py",
        "install_bundle.py",
        "offline_self_test.py",
        "AGENT_START_HERE.md",
        "INSTALL_PROMPT.md",
        "INSTALLATION_REPORT.md",
        "DEPENDENCY_MATRIX.md",
        "ROLLBACK_REPORT.md",
        "E2E_REPORT.md",
        "CHANGELOG.md",
        "agent-handoff.json",
    ):
        _copy_file(deploy / name, staging / name)


def render_release_metadata(staging: Path) -> None:
    """Render the single source version into release handoff files."""

    for relative in (
        "AGENT_START_HERE.md",
        "INSTALLATION_REPORT.md",
        "agent-handoff.json",
    ):
        path = staging / relative
        content = path.read_text(encoding="utf-8")
        if "@@VERSION@@" not in content:
            raise RuntimeError(f"release version placeholder is missing: {relative}")
        path.write_text(content.replace("@@VERSION@@", VERSION), encoding="utf-8")


def audit(staging: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    failures: list[str] = []
    for path in sorted(staging.rglob("*")):
        relative = path.relative_to(staging).as_posix()
        if path.is_symlink():
            failures.append(f"symlink:{relative}")
            continue
        if not path.is_file():
            continue
        if any(part in SKIP_NAMES for part in path.parts) or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            failures.append(f"forbidden:{relative}")
            continue
        data = path.read_bytes()
        if len(data) <= 8 * 1024 * 1024:
            if any(pattern.search(data) for pattern in SECRET_PATTERNS):
                failures.append(f"secret:{relative}")
            if LOCAL_PATH.search(data):
                failures.append(f"local_path:{relative}")
        files[relative] = hash_file(path)
    if failures:
        raise RuntimeError("release audit failed: " + ", ".join(failures))
    return files


def normalize_metadata(root: Path, epoch: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (epoch, epoch), follow_symlinks=False)
        if path.is_file():
            path.chmod(0o755 if path.suffix == ".py" and (path.parent.name == "scripts" or path.parent == root) else 0o644)
        elif path.is_dir():
            path.chmod(0o755)
    os.utime(root, (epoch, epoch))


def build_zip(root: Path, destination: Path, epoch: int) -> None:
    timestamp = time.gmtime(epoch)[:6]
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = Path(root.name) / path.relative_to(root)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=timestamp)
            info.create_system = 3
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info.external_attr = (mode & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def manifest(staging: Path, files: dict[str, str], epoch: int) -> dict[str, Any]:
    return {
        "manifest_version": "people-tracking-feishu-release-v1",
        "release": staging.name,
        "version": VERSION,
        "created_at": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
        "source_date_epoch": epoch,
        "entrypoints": {
            "verify": "python3 verify_release.py",
            "self_test": "python3 offline_self_test.py",
            "doctor": "python3 install_bundle.py --doctor",
            "dry_run": "python3 install_bundle.py --dry-run",
            "apply": "python3 install_bundle.py --apply",
            "runtime": "people-tracking-feishu",
            "source_routes_preview": "people-tracking-feishu source-routes --json",
            "strict_homepage_scan": (
                "people-tracking-feishu scan --force-all --force-full-fetch "
                "--source-kind homepage --homepage-retries 1 "
                "--homepage-backoff-seconds 1.0 --max-error-rate 0.10 --json"
            ),
        },
        "dependencies": {
            "python": ["3.11", "3.12"],
            "node": ">=16",
            "lark_cli": "1.0.82",
            "lark_cli_npm_integrity": PINNED_NPM_INTEGRITY,
            "lark_skills": ["lark-shared", "lark-doc", "lark-base", "lark-im"],
            "bundled_python_wheels": ["pypinyin==0.55.0"],
            "installer_network_access": False,
        },
        "runtime": {
            "adapters": ["openclaw_official_feishu_plugin", "claude_lark_cli"],
            "release_root": "~/.local/share/people-tracking-feishu/releases/<version>",
            "config_root": "~/.config/people-tracking-feishu",
            "state_root": "~/.local/state/people-tracking-feishu",
            "database": "SQLite",
        },
        "data_policy": {
            "real_people_data_included": False,
            "historical_snapshots_included": False,
            "runtime_databases_included": False,
            "credentials_included": False,
            "local_absolute_paths_included": False,
            "synthetic_test_data_only": True,
        },
        "deepseek_policy": {
            "default_enabled": False,
            "model": "deepseek-v4-flash",
            "eligible_use_cases": ["ambiguous_review", "confirmed_summary"],
            "full_pages_sent": False,
            "key_transport": "mode-0600-file-or-secret-reference",
            "real_smoke_requires_confirmation": True,
        },
        "install_policy": {
            "global_editable_pip": False,
            "external_dependency_auto_install": False,
            "existing_agent_config_modified": False,
            "existing_feishu_config_modified": False,
            "schedules_installed_before_onboarding_confirmation": False,
            "same_name_skill_backup": True,
        },
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--source-date-epoch", type=int, default=SOURCE_DATE_EPOCH)
    args = parser.parse_args()
    if args.version != VERSION:
        raise SystemExit(f"this builder is pinned to {VERSION}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="people-tracking-feishu-build-", dir=ROOT) as temporary:
        staging = Path(temporary) / f"{RELEASE_PREFIX}-{args.version}"
        staging.mkdir()
        stage_release(staging)
        render_release_metadata(staging)
        files = audit(staging)
        release_manifest = manifest(staging, files, args.source_date_epoch)
        (staging / "release-manifest.json").write_text(
            json.dumps(release_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        normalize_metadata(staging, args.source_date_epoch)
        archive = args.output_dir / f"{staging.name}.zip"
        build_zip(staging, archive, args.source_date_epoch)
    checksum = hash_file(archive)
    checksum_path = archive.with_suffix(archive.suffix + ".sha256")
    checksum_path.write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "archive": str(archive),
                "sha256": checksum,
                "checksum_file": str(checksum_path),
                "file_count": len(files) + 1,
                "secrets_included": False,
                "real_people_data_included": False,
                "runtime_databases_included": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
