#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


FORBIDDEN_NAMES = {
    ".env",
    "api-token",
    "runtime.env",
    "postgres-password",
    "deepseek-api-key",
    "cookies.json",
    "cookie.json",
    "client.json",
}
FORBIDDEN_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".csv",
    ".tsv",
    ".xlsx",
    ".xls",
    ".parquet",
    ".jsonl",
    ".pem",
    ".key",
}
FORBIDDEN_PARTS = {"__pycache__", ".git", ".venv", "node_modules", "artifacts", "inputs"}
MAX_AUDITED_FILE_BYTES = 8 * 1024 * 1024
SECRET_PATTERNS = {
    "deepseek_or_openai_key": re.compile(rb"\bsk-[A-Za-z0-9]{24,}\b"),
    "bearer_token": re.compile(rb"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    "feishu_app_secret": re.compile(rb"(?i)(?:app[_-]?secret)\s*[:=]\s*[\"']?[A-Za-z0-9_-]{20,}"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
FORBIDDEN_DEEPSEEK_RUNTIME_PATTERNS = {
    "deepseek_endpoint": re.compile(rb"(?i)api\.deepseek\.com"),
    "deepseek_environment": re.compile(rb"PEOPLE_INTEL_DEEPSEEK_"),
    "deepseek_enable_flag": re.compile(rb"(?<!no-)--deepseek(?:[\s\"'])"),
    "deepseek_runtime_module": re.compile(rb"(?i)deepseek_fallback"),
    "deepseek_runtime_reviewer": re.compile(rb"(?i)DeepSeek(?:Diff)?Reviewer"),
}
LOCAL_PATH = re.compile(rb"/(?:Users|home)/[A-Za-z0-9._-]+/")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root: Path) -> dict[str, Any]:
    root = root.resolve()
    version_path = root / "VERSION"
    if not version_path.is_file():
        raise ValueError("VERSION is missing")
    version = version_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+-portable\.\d+", version):
        raise ValueError("VERSION is invalid")
    manifest_path = root / "release-manifest.json"
    if not manifest_path.is_file():
        raise ValueError("release-manifest.json is missing")
    if manifest_path.stat().st_size > MAX_AUDITED_FILE_BYTES:
        raise ValueError("release-manifest.json exceeds the safe verification size limit")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != "people-tracking-feishu-release-v1":
        raise ValueError("unsupported release manifest version")
    audit_policy = manifest.get("release_audit_policy") or {}
    if (
        audit_policy.get("content_scan_is_mandatory") is not True
        or audit_policy.get("maximum_regular_file_bytes") != MAX_AUDITED_FILE_BYTES
        or audit_policy.get("oversized_file_action") != "reject"
    ):
        raise ValueError("release manifest does not declare the required content-scan policy")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("manifest files map is empty")
    actual: dict[str, str] = {}
    scan: dict[str, list[str]] = {
        "secrets": [],
        "deepseek_runtime": [],
        "local_paths": [],
        "forbidden": [],
        "oversized": [],
        "symlinks": [],
    }
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            scan["symlinks"].append(relative)
            continue
        if not path.is_file() or relative == "release-manifest.json":
            continue
        lowered = path.name.casefold()
        relative_parts = path.relative_to(root).parts
        if (
            lowered in FORBIDDEN_NAMES
            or path.suffix.casefold() in FORBIDDEN_SUFFIXES
            or any(part in FORBIDDEN_PARTS for part in relative_parts)
        ):
            scan["forbidden"].append(relative)
        size = path.stat().st_size
        if size > MAX_AUDITED_FILE_BYTES:
            scan["oversized"].append(
                f"{relative}:{size}>{MAX_AUDITED_FILE_BYTES}"
            )
            actual[relative] = sha256(path)
            continue
        data = path.read_bytes()
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                scan["secrets"].append(f"{relative}:{name}")
        if relative.startswith(("runtime/", "full-source/src/")):
            for name, pattern in FORBIDDEN_DEEPSEEK_RUNTIME_PATTERNS.items():
                if pattern.search(data):
                    scan["deepseek_runtime"].append(f"{relative}:{name}")
        if LOCAL_PATH.search(data):
            scan["local_paths"].append(relative)
        actual[relative] = sha256(path)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = sorted(path for path in set(actual) & set(expected) if actual[path] != expected[path])
    version_matches = (
        manifest.get("version") == version
        and manifest.get("release") == f"people-tracking-feishu-{version}"
    )
    ok = (
        version_matches
        and not any(scan.values())
        and not missing
        and not extra
        and not mismatched
    )
    return {
        "ok": ok,
        "release": manifest.get("release"),
        "version": version,
        "version_matches": version_matches,
        "file_count": len(actual),
        "manifest_files_match": not missing and not extra and not mismatched,
        "missing": missing,
        "extra": extra,
        "mismatched": mismatched,
        "scan": scan,
        "policies": {
            "secrets_included": False,
            "real_people_data_included": False,
            "runtime_databases_included": False,
            "local_absolute_paths_included": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    try:
        report = verify(args.root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
