#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _release_version() -> str:
    here = Path(__file__).resolve()
    candidates = (here.parent / "VERSION", here.parents[2] / "VERSION")
    for candidate in candidates:
        if candidate.is_file():
            value = candidate.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"\d+\.\d+\.\d+-portable\.\d+", value):
                return value
    raise RuntimeError("release VERSION file is missing or invalid")


VERSION = _release_version()
PINNED_LARK_CLI = "1.0.82"
REQUIRED_LARK_SKILLS = ("lark-shared", "lark-doc", "lark-base", "lark-im")
SKIP_COPY = {"__pycache__", ".DS_Store"}


class InstallFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        mutated: bool,
        mutations: list[str],
        rollback_result: dict[str, Any] | None,
    ):
        super().__init__(message)
        self.mutated = mutated
        self.mutations = mutations
        self.rollback_result = rollback_result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        if any(part in SKIP_COPY for part in path.parts):
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def secure_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    path.chmod(0o600)


def _version(executable: str, args: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=15, check=False
        )
        return result.returncode, (result.stdout or result.stderr).strip()[:1000]
    except Exception as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def _semver(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    return tuple(map(int, match.groups())) if match else None


def _python_candidates(explicit: str | None) -> list[str]:
    values = [explicit, sys.executable, shutil.which("python3.12"), shutil.which("python3.11")]
    return list(dict.fromkeys(value for value in values if value))


def choose_python(explicit: str | None) -> dict[str, Any]:
    inspected = []
    for executable in _python_candidates(explicit):
        code = "import json,sys;print(json.dumps({'version':list(sys.version_info[:3]),'executable':sys.executable}))"
        result = subprocess.run(
            [executable, "-c", code], capture_output=True, text=True, timeout=10, check=False
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
        supported = result.returncode == 0 and tuple(payload.get("version", [])[:2]) in {(3, 11), (3, 12)}
        inspected.append({"requested": executable, "resolved": payload.get("executable"), "version": payload.get("version"), "supported": supported})
        if supported:
            return {"ok": True, "selected": payload["executable"], "inspected": inspected}
    return {"ok": False, "selected": None, "inspected": inspected}


def detect_openclaw_plugin() -> dict[str, Any]:
    executable = shutil.which("openclaw")
    if not executable:
        return {"available": False, "official_feishu_plugin": False}
    result = subprocess.run(
        [executable, "plugins", "list", "--json"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    text = result.stdout.casefold()
    return {
        "available": result.returncode == 0,
        "executable": executable,
        "official_feishu_plugin": result.returncode == 0 and "feishu" in text,
        "probe_returncode": result.returncode,
    }


def find_lark_skills(home: Path) -> dict[str, str | None]:
    roots = [
        home / ".claude" / "skills",
        home / ".agents" / "skills",
        home / ".openclaw" / "skills",
        home / ".codex" / "skills",
    ]
    found: dict[str, str | None] = {}
    for name in REQUIRED_LARK_SKILLS:
        path = next((root / name for root in roots if (root / name / "SKILL.md").is_file()), None)
        found[name] = str(path) if path else None
    return found


def dependency_report(args: argparse.Namespace, home: Path) -> dict[str, Any]:
    python = choose_python(args.python)
    node_executable = shutil.which("node")
    node_code, node_output = _version(node_executable, ["--version"]) if node_executable else (127, "missing")
    node_version = _semver(node_output)
    node_ok = node_code == 0 and node_version is not None and node_version[0] >= 16
    lark_executable = args.lark_cli or shutil.which("lark-cli")
    lark_code, lark_output = _version(lark_executable, ["--version"]) if lark_executable else (127, "missing")
    lark_version = _semver(lark_output)
    lark_ok = lark_code == 0 and lark_version == (1, 0, 82)
    openclaw = detect_openclaw_plugin()
    runtime_mode = args.runtime_mode
    if runtime_mode == "auto":
        runtime_mode = "openclaw" if openclaw.get("official_feishu_plugin") else "claude_lark_cli"
    skills = find_lark_skills(home)
    skills_ok = all(skills.values())
    if runtime_mode == "openclaw":
        adapter_ok = bool(openclaw.get("official_feishu_plugin"))
    else:
        adapter_ok = node_ok and lark_ok and skills_ok
    missing = []
    if not python["ok"]:
        missing.append("Python 3.11 or 3.12")
    if runtime_mode == "openclaw" and not adapter_ok:
        missing.append("OpenClaw official Feishu plugin")
    if runtime_mode == "claude_lark_cli":
        if not node_ok:
            missing.append("Node.js >=16")
        if not lark_ok:
            missing.append(f"lark-cli {PINNED_LARK_CLI}")
        if not skills_ok:
            missing.extend(name for name, path in skills.items() if not path)
    return {
        "ok": python["ok"] and adapter_ok,
        "runtime_mode": runtime_mode,
        "python": python,
        "node": {"executable": node_executable, "version_output": node_output, "supported": node_ok},
        "lark_cli": {
            "executable": lark_executable,
            "version_output": lark_output,
            "required": PINNED_LARK_CLI,
            "supported": lark_ok,
        },
        "lark_skills": skills,
        "openclaw": openclaw,
        "missing": missing,
        "official_remediation": [
            "npx @larksuite/cli@1.0.82 install",
            "npx skills add larksuite/cli -g -y",
            "Restart the AI Agent so the official lark-* skills are reloaded.",
        ],
        "network_install_performed": False,
        "secret_values_read": False,
    }


def verify_release(
    root: Path, *, trusted_verifier_root: Path | None = None
) -> dict[str, Any]:
    verifier_root = trusted_verifier_root or root
    result = subprocess.run(
        [
            sys.executable,
            str(verifier_root / "verify_release.py"),
            "--root",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("release verifier did not return JSON") from exc
    if result.returncode != 0 or not payload.get("ok"):
        raise RuntimeError("release verification failed")
    return payload


def offline_self_test(root: Path, python_executable: str) -> dict[str, Any]:
    wheel_paths = sorted((root / "vendor" / "wheels").glob("*.whl"))
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root / "runtime"), *(str(path) for path in wheel_paths)]
    )
    result = subprocess.run(
        [python_executable, str(root / "offline_self_test.py")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"offline self-test did not return JSON: {detail}") from exc
    if result.returncode != 0 or not payload.get("ok"):
        raise RuntimeError("offline synthetic self-test failed")
    return payload


def skill_destinations(home: Path) -> list[Path]:
    destinations: list[Path] = []
    if (home / ".openclaw").exists() or shutil.which("openclaw"):
        destinations.append(home / ".openclaw" / "skills" / "people-tracking")
    if (home / ".claude").exists() or shutil.which("claude"):
        destinations.append(home / ".claude" / "skills" / "people-tracking")
    return destinations


def plan(args: argparse.Namespace, root: Path, home: Path) -> dict[str, Any]:
    dependencies = dependency_report(args, home)
    share = home / ".local" / "share" / "people-tracking-feishu"
    config = home / ".config" / "people-tracking-feishu"
    state = home / ".local" / "state" / "people-tracking-feishu"
    release = share / "releases" / VERSION
    launcher = home / ".local" / "bin" / "people-tracking-feishu"
    skills = skill_destinations(home)
    collisions = {
        "release_exists": release.exists(),
        "launcher_exists": launcher.exists(),
        "skills": [
            {
                "path": str(path),
                "exists": path.exists(),
                "same_hash": path.exists()
                and tree_hash(path) == tree_hash(root / "packages" / "feishu" / "people-tracking"),
            }
            for path in skills
        ],
    }
    return {
        "release": VERSION,
        "runtime_mode": dependencies["runtime_mode"],
        "dependencies": dependencies,
        "paths": {
            "release": str(release),
            "config": str(config),
            "state": str(state),
            "venv": str(state / "venvs" / VERSION),
            "launcher": str(launcher),
            "skills": [str(path) for path in skills],
        },
        "collisions": collisions,
        "scheduled_jobs_added_by_installer": [],
        "existing_agent_or_feishu_config_modified": False,
        "rollback_state": str(config / "install-state.json"),
        "ready_to_apply": dependencies["ok"] and bool(skills),
    }


def _copy_tree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"symlink rejected: {path}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*SKIP_COPY),
        copy_function=shutil.copy2,
    )


def _backup_name(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.backup-{stamp}")


def _build_and_replace_private_venv(
    *, venv: Path, selected_python: str, release: Path
) -> Path | None:
    """Build from a trusted interpreter without executing an existing venv.

    Existing versioned venvs are untrusted mutable state.  They are renamed to a
    quarantine path only after a complete replacement has passed dependency
    checks; the installer never invokes their ``bin/python``.
    """

    venv.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{venv.name}.", dir=venv.parent))
    python_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PIP_"))
    }
    python_environment["PYTHONNOUSERSITE"] = "1"
    wheel = release / "vendor" / "wheels" / "pypinyin-0.55.0-py2.py3-none-any.whl"
    if wheel.is_symlink() or not wheel.is_file():
        raise RuntimeError("verified bundled pypinyin wheel is missing")
    quarantine: Path | None = None
    try:
        subprocess.run(
            [selected_python, "-m", "venv", str(staging)],
            check=True,
            timeout=120,
            capture_output=True,
            text=True,
            env=python_environment,
        )
        staged_python = staging / "bin" / "python"
        pip_environment = {**python_environment, "PIP_NO_INDEX": "1"}
        subprocess.run(
            [
                str(staged_python),
                "-m",
                "pip",
                "install",
                "--isolated",
                "--no-index",
                "--no-deps",
                "--disable-pip-version-check",
                str(wheel),
            ],
            check=True,
            timeout=120,
            capture_output=True,
            text=True,
            env=pip_environment,
        )
        subprocess.run(
            [
                str(staged_python),
                "-c",
                "import pypinyin; assert pypinyin.__version__ == '0.55.0'",
            ],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
            env=pip_environment,
        )
        if os.path.lexists(venv):
            quarantine = _backup_name(venv)
            quarantine = quarantine.with_name(
                quarantine.name.replace(".backup-", ".quarantine-", 1)
            )
            venv.rename(quarantine)
        try:
            staging.rename(venv)
        except Exception:
            if quarantine is not None and quarantine.exists() and not os.path.lexists(venv):
                quarantine.rename(venv)
            raise
        return quarantine
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _install_skill(source: Path, destination: Path) -> dict[str, Any]:
    source_hash = tree_hash(source)
    if destination.exists() and tree_hash(destination) == source_hash:
        return {"path": str(destination), "changed": False, "backup": None, "hash": source_hash}
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = _backup_name(destination) if destination.exists() else None
    staging = Path(tempfile.mkdtemp(prefix=".people-tracking-", dir=destination.parent))
    try:
        _copy_tree(source, staging / destination.name)
        if backup:
            destination.rename(backup)
        try:
            (staging / destination.name).rename(destination)
        except Exception:
            if backup and backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for script in (destination / "scripts").glob("*.py"):
        script.chmod(0o755)
    return {"path": str(destination), "changed": True, "backup": str(backup) if backup else None, "hash": source_hash}


def _apply_install_unchecked(args: argparse.Namespace, root: Path, home: Path, preview: dict[str, Any]) -> dict[str, Any]:
    if not preview["ready_to_apply"]:
        raise RuntimeError("dependencies are not ready; no installation changes were made")
    self_test = offline_self_test(
        root,
        str(preview["dependencies"]["python"]["selected"]),
    )
    release = Path(preview["paths"]["release"])
    config = Path(preview["paths"]["config"])
    state = Path(preview["paths"]["state"])
    venv = Path(preview["paths"]["venv"])
    launcher = Path(preview["paths"]["launcher"])
    install_state_path = config / "install-state.json"
    previous_state: dict[str, Any] = {}
    if install_state_path.is_file():
        try:
            previous_state = json.loads(install_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise RuntimeError("existing install-state.json is invalid")
    if release.exists():
        installed_manifest = release / "release-manifest.json"
        if not installed_manifest.is_file() or sha256(installed_manifest) != sha256(root / "release-manifest.json"):
            raise RuntimeError("release destination already exists with different contents")
        # Never execute the verifier from the already-installed tree: that is
        # precisely the tree whose integrity is in question.  The incoming
        # release was verified before apply and supplies the trusted verifier
        # used to hash every installed file and reject extras/symlinks/caches.
        verify_release(release, trusted_verifier_root=root)
    else:
        release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = release.with_name(f".{release.name}.{os.getpid()}.tmp")
        _copy_tree(root, staging)
        staging.rename(release)
    # Installed-tree verification is a precondition for all configuration/state
    # mutations.  A pre-seeded corrupt release must fail with mutated=false and
    # must not create or chmod unrelated runtime directories.
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.chmod(0o700)
    state.chmod(0o700)
    venv_quarantine = _build_and_replace_private_venv(
        venv=venv,
        selected_python=str(preview["dependencies"]["python"]["selected"]),
        release=release,
    )
    skills = [
        _install_skill(
            release / "packages" / "feishu" / "people-tracking",
            Path(destination),
        )
        for destination in preview["paths"]["skills"]
    ]
    previous_skills = {
        str(item.get("path")): item
        for item in previous_state.get("skills", [])
        if isinstance(item, dict)
    }
    for item in skills:
        previous = previous_skills.get(item["path"])
        if previous and not item.get("changed") and not item.get("backup"):
            item["backup"] = previous.get("backup")
            item["changed"] = bool(
                previous.get("changed") or previous.get("backup")
            )
    launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher_backup = None
    launcher_changed = False
    launcher_content = (
        "#!/bin/sh\n"
        "unset PYTHONHOME\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        "export PYTHONNOUSERSITE=1\n"
        "export PYTHONSAFEPATH=1\n"
        f"export PYTHONPATH={shlex.quote(str(release / 'runtime'))}\n"
        f"exec {shlex.quote(str(venv / 'bin' / 'python'))} -P -m people_tracking_feishu.cli \"$@\"\n"
    )
    if launcher.exists():
        current = launcher.read_text(encoding="utf-8")
        if current != launcher_content:
            launcher_backup = _backup_name(launcher)
            launcher.rename(launcher_backup)
            launcher_changed = True
    if not launcher.exists():
        secure_write(launcher, launcher_content)
        launcher.chmod(0o700)
        launcher_changed = True
    if launcher_backup is None:
        launcher_backup = (
            Path(previous_state["launcher_backup"])
            if previous_state.get("launcher_backup")
            else None
        )
        launcher_changed = bool(
            launcher_changed
            or previous_state.get("launcher_changed")
            or launcher_backup
        )
    install_state = {
        "schema_version": "people-tracking-feishu-install-state-v1",
        "version": VERSION,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "status": "installed",
        "release": str(release),
        "venv": str(venv),
        "venv_quarantine": str(venv_quarantine) if venv_quarantine else None,
        "venv_rebuilt_from_trusted_python": True,
        "launcher": str(launcher),
        "launcher_backup": str(launcher_backup) if launcher_backup else None,
        "launcher_changed": launcher_changed,
        "skills": skills,
        "runtime_mode": preview["runtime_mode"],
        "scheduled_jobs": [],
        "external_config_modified": False,
        "secrets_read": False,
    }
    secure_write(install_state_path, json.dumps(install_state, ensure_ascii=False, indent=2) + "\n")
    smoke = subprocess.run(
        [str(launcher), "doctor", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if smoke.returncode != 0:
        detail = (smoke.stderr or smoke.stdout).strip()[-2000:]
        raise RuntimeError(f"installed launcher doctor failed: {detail}")
    return {
        "installed": True,
        "state": install_state,
        "offline_self_test": self_test,
        "doctor": json.loads(smoke.stdout),
        "restart_agent_required": True,
        "next": "restart/reload skills, then run people-tracking-feishu onboarding --json",
    }


def _path_snapshot(path: Path, *, metadata_only: bool = False) -> dict[str, Any]:
    if path.is_symlink():
        return {
            "exists": True,
            "kind": "symlink",
            "link_target": os.readlink(path),
            "mode": stat.S_IMODE(path.lstat().st_mode),
            "metadata_only": metadata_only,
            "tree_hash": None,
            "sha256": None,
        }
    return {
        "exists": path.exists(),
        "kind": "directory" if path.is_dir() else "file" if path.is_file() else None,
        "mode": stat.S_IMODE(path.stat().st_mode) if path.exists() else None,
        "metadata_only": metadata_only,
        "tree_hash": tree_hash(path) if path.is_dir() and not metadata_only else None,
        "sha256": sha256(path) if path.is_file() and not metadata_only else None,
    }


def _latest_backup(path: Path) -> Path | None:
    matches = sorted(path.parent.glob(f"{path.name}.backup-*")) if path.parent.exists() else []
    return matches[-1] if matches else None


def _latest_quarantine(path: Path) -> Path | None:
    matches = (
        sorted(path.parent.glob(f"{path.name}.quarantine-*"))
        if path.parent.exists()
        else []
    )
    return matches[-1] if matches else None


def _failed_install_state(
    preview: dict[str, Any],
    before: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    release = Path(preview["paths"]["release"])
    venv = Path(preview["paths"]["venv"])
    launcher = Path(preview["paths"]["launcher"])
    mutations: list[str] = []
    skills: list[dict[str, Any]] = []
    for raw in preview["paths"]["skills"]:
        target = Path(raw)
        prior = before[str(target)]
        current = _path_snapshot(target)
        changed = current != prior
        if changed:
            mutations.append(str(target))
        backup = _latest_backup(target) if changed and prior["exists"] else None
        skills.append(
            {
                "path": str(target),
                "changed": changed,
                "backup": str(backup) if backup else None,
            }
        )
    launcher_changed = _path_snapshot(launcher) != before[str(launcher)]
    if launcher_changed:
        mutations.append(str(launcher))
    venv_changed = _path_snapshot(venv) != before[str(venv)]
    for target in (release, venv):
        if _path_snapshot(target) != before[str(target)]:
            mutations.append(str(target))
    for target in (
        Path(preview["paths"]["config"]),
        Path(preview["paths"]["state"]),
    ):
        if _path_snapshot(target, metadata_only=True) != before[str(target)]:
            mutations.append(str(target))
    return (
        {
            "schema_version": "people-tracking-feishu-install-state-v1",
            "version": VERSION,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "status": "install_failed",
            "release": str(release),
            "venv": str(venv),
            "venv_quarantine": (
                str(_latest_quarantine(venv))
                if before[str(venv)]["exists"] and _latest_quarantine(venv)
                else None
            ),
            "venv_rebuilt_from_trusted_python": venv_changed,
            "launcher": str(launcher),
            "launcher_backup": (
                str(_latest_backup(launcher)) if before[str(launcher)]["exists"] and _latest_backup(launcher) else None
            ),
            "launcher_changed": launcher_changed,
            "skills": skills,
            "runtime_mode": preview["runtime_mode"],
            "scheduled_jobs": [],
            "external_config_modified": False,
            "secrets_read": False,
        },
        mutations,
    )


def apply_install(args: argparse.Namespace, root: Path, home: Path, preview: dict[str, Any]) -> dict[str, Any]:
    targets = [
        Path(preview["paths"]["release"]),
        Path(preview["paths"]["venv"]),
        Path(preview["paths"]["launcher"]),
        *(Path(path) for path in preview["paths"]["skills"]),
    ]
    before = {str(path): _path_snapshot(path) for path in targets}
    for path in (
        Path(preview["paths"]["config"]),
        Path(preview["paths"]["state"]),
    ):
        before[str(path)] = _path_snapshot(path, metadata_only=True)
    try:
        return _apply_install_unchecked(args, root, home, preview)
    except Exception as exc:
        try:
            state, mutations = _failed_install_state(preview, before)
        except Exception as audit_exc:
            raise InstallFailure(
                f"{exc}; mutation audit failed: {audit_exc}",
                mutated=True,
                mutations=["mutation_audit_incomplete"],
                rollback_result={
                    "status": "failed",
                    "error": f"mutation audit failed: {audit_exc}",
                },
            ) from exc
        rollback_result: dict[str, Any] | None = None
        if mutations:
            install_state_path = Path(preview["paths"]["config"]) / "install-state.json"
            try:
                secure_write(
                    install_state_path,
                    json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                )
                if str(install_state_path) not in mutations:
                    mutations.append(str(install_state_path))
                config_path = Path(preview["paths"]["config"])
                if (
                    _path_snapshot(config_path, metadata_only=True)
                    != before[str(config_path)]
                    and str(config_path) not in mutations
                ):
                    mutations.append(str(config_path))
            except Exception as state_exc:
                rollback_result = {
                    "status": "failed",
                    "error": f"failed to persist rollback state: {state_exc}",
                }
            else:
                try:
                    rollback_result = rollback(home)
                except Exception as rollback_exc:
                    rollback_result = {
                        "status": "failed",
                        "error": f"automatic rollback failed: {rollback_exc}",
                    }
        raise InstallFailure(
            str(exc),
            mutated=bool(mutations),
            mutations=mutations,
            rollback_result=rollback_result,
        ) from exc


def rollback(home: Path) -> dict[str, Any]:
    path = home / ".config" / "people-tracking-feishu" / "install-state.json"
    if not path.is_file():
        raise RuntimeError("install-state.json is missing")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("rollback", {}).get("status") == "completed":
        return {
            "rolled_back": True,
            "idempotent_replay": True,
            "actions": [],
            "install_state": str(path),
            "rollback": state["rollback"],
        }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    actions = []
    for item in state.get("skills", []):
        target = Path(item["path"])
        if target.exists() and item.get("changed"):
            recovery = target.with_name(f"{target.name}.rolledback-{stamp}")
            target.rename(recovery)
            actions.append({"moved": str(target), "to": str(recovery)})
        backup = (
            Path(item["backup"])
            if item.get("changed") and item.get("backup")
            else None
        )
        if backup and backup.exists() and not target.exists():
            backup.rename(target)
            actions.append({"restored": str(target), "from": str(backup)})
    launcher = Path(state["launcher"])
    if launcher.exists() and state.get("launcher_changed", True):
        recovery = launcher.with_name(f"{launcher.name}.rolledback-{stamp}")
        launcher.rename(recovery)
        actions.append({"moved": str(launcher), "to": str(recovery)})
    backup = Path(state["launcher_backup"]) if state.get("launcher_backup") else None
    if backup and backup.exists() and not launcher.exists():
        backup.rename(launcher)
        actions.append({"restored": str(launcher), "from": str(backup)})
    rollback_record = {
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "actions": actions,
    }
    state["status"] = "rolled_back"
    state["rollback"] = rollback_record
    secure_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return {
        "rolled_back": True,
        "idempotent_replay": False,
        "actions": actions,
        "install_state": str(path),
        "rollback": rollback_record,
        "preserved": [
            value
            for value in (
                state.get("release"),
                state.get("venv"),
                state.get("venv_quarantine"),
                str(path.parent),
                str(home / ".local/state/people-tracking-feishu"),
            )
            if value
        ],
        "deleted": [],
        "scheduled_jobs_note": "Schedules are registered only after onboarding confirmation and require their own explicit removal.",
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    action = root.add_mutually_exclusive_group(required=True)
    action.add_argument("--doctor", action="store_true")
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    root.add_argument("--home", type=Path, default=Path.home())
    root.add_argument("--python")
    root.add_argument("--lark-cli")
    root.add_argument("--runtime-mode", choices=("auto", "openclaw", "claude_lark_cli"), default="auto")
    return root


def main() -> int:
    args = parser().parse_args()
    root = Path(__file__).resolve().parent
    home = args.home.expanduser().resolve()
    try:
        if args.rollback:
            payload = rollback(home)
        else:
            verified = verify_release(root)
            preview = plan(args, root, home)
            payload = {
                "ok": True,
                "action": "doctor" if args.doctor else "dry-run" if args.dry_run else "apply",
                "release_verification": verified,
                "plan": preview,
                "mutated": False,
            }
            if args.apply:
                payload["result"] = apply_install(args, root, home, preview)
                payload["mutated"] = True
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        mutated = bool(getattr(exc, "mutated", False))
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                    "mutated": mutated,
                    "mutations": list(getattr(exc, "mutations", [])),
                    "rollback": getattr(exc, "rollback_result", None),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
