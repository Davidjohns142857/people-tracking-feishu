from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from people_intel.deepseek_fallback import DeepSeekDiffReviewer, DeepSeekPolicyError
from people_intel.light_cli import _public_urlopen
from people_intel.light_tracker import ChangeDecision

from . import __version__
from .config import (
    QUESTIONNAIRE,
    ConfigError,
    RuntimePaths,
    load_config,
    mark_state,
    normalize_answers,
    secure_write_json,
    utc_now,
    validate_config,
)
from .ingest import canonical_records, records_from_file, records_from_payload
from .lark import LarkCli, LarkCliError, PINNED_LARK_CLI, first_value
from .master import create_master_base, master_schema, sync_visible_master, visible_records
from .sandbox import algorithm_e2e, feishu_e2e
from .scheduler import install_local_scheduler, openclaw_cron_plan
from .state import PortableState
from .tracking import (
    deepseek_public_status,
    deliver_digest,
    materialize_deepseek_config,
    scan_due,
)


ENABLE_PHRASE = "确认启用"
SANDBOX_PHRASE = "确认运行私有沙箱E2E"
DEEPSEEK_PHRASE = "确认运行DeepSeek烟测"


def _nonnegative_finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return parsed


def _json_file(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input must be a regular non-symlink file")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("input exceeds the 20 MiB safety limit")
    return json.loads(path.read_text(encoding="utf-8"))


def _bridge_refs_hash(refs: list[str]) -> str:
    encoded = json.dumps(sorted(refs), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bridge_request_hash(*, purpose: str, content: Any) -> str:
    encoded = json.dumps(
        {"purpose": purpose, "content": content},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _new_bridge_request(
    paths: RuntimePaths,
    *,
    purpose: str,
    expected_refs: list[str],
    request_content: Any,
    state: PortableState | None = None,
) -> dict[str, Any]:
    refs = sorted(str(ref) for ref in expected_refs)
    if not refs or len(refs) != len(set(refs)):
        raise ValueError("bridge request must contain unique expected refs")
    request_hash = _bridge_request_hash(purpose=purpose, content=request_content)
    owned = state is None
    active = state or PortableState(paths.database)
    try:
        existing = active.get_meta(f"bridge_request:{purpose}")
        if (
            isinstance(existing, dict)
            and existing.get("status") == "pending"
            and existing.get("expected_refs") == refs
            and existing.get("expected_refs_hash") == _bridge_refs_hash(refs)
            and existing.get("request_hash") == request_hash
        ):
            return existing
        request = {
            "schema_version": "people-tracking-bridge-v1",
            "purpose": purpose,
            "bridge_nonce": secrets.token_hex(16),
            "expected_refs": refs,
            "expected_refs_hash": _bridge_refs_hash(refs),
            "request_hash": request_hash,
            "created_at": utc_now(),
            "status": "pending",
        }
        active.set_meta(f"bridge_request:{purpose}", request)
        return request
    finally:
        if owned:
            active.close()


def _bridge_public_fields(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: request[key]
        for key in (
            "schema_version",
            "purpose",
            "bridge_nonce",
            "expected_refs",
            "expected_refs_hash",
            "request_hash",
        )
    }


def _validate_bridge_result(
    paths: RuntimePaths,
    payload: Any,
    *,
    purpose: str,
    completed_refs: list[str],
    state: PortableState | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("all_ok") is not True:
        raise ValueError("bridge result must explicitly contain all_ok=true")
    owned = state is None
    active = state or PortableState(paths.database)
    try:
        request = active.get_meta(f"bridge_request:{purpose}")
        if not isinstance(request, dict) or request.get("status") != "pending":
            raise ValueError(f"no pending {purpose} bridge request exists")
        expected = list(request.get("expected_refs") or [])
        echoed = payload.get("expected_refs")
        if payload.get("bridge_nonce") != request.get("bridge_nonce"):
            raise ValueError("bridge result nonce does not match the pending request")
        if echoed != expected:
            raise ValueError("bridge result expected_refs do not match the pending request")
        if payload.get("expected_refs_hash") != request.get("expected_refs_hash"):
            raise ValueError("bridge result expected_refs_hash does not match the pending request")
        if payload.get("request_hash") != request.get("request_hash"):
            raise ValueError("bridge result request_hash does not match the pending request")
        actual = [str(ref) for ref in completed_refs]
        if len(actual) != len(set(actual)):
            raise ValueError("bridge result contains duplicate refs")
        missing = sorted(set(expected) - set(actual))
        unknown = sorted(set(actual) - set(expected))
        if missing or unknown:
            raise ValueError(
                "bridge result coverage mismatch: "
                f"missing={missing or []}, unknown={unknown or []}"
            )
        consumed = {
            **request,
            "status": "consumed",
            "consumed_at": utc_now(),
        }
        active.set_meta(f"bridge_request:{purpose}", consumed)
        return consumed
    finally:
        if owned:
            active.close()


def _bridge_actions(actions: list[dict[str, Any]], request: dict[str, Any]) -> list[dict[str, Any]]:
    fields = _bridge_public_fields(request)
    return [{**action, **fields} for action in actions]


def _master_expected_refs(records: dict[str, list[dict[str, Any]]]) -> list[str]:
    refs = ["table:People", "table:Sources"]
    for table in ("People", "Sources"):
        refs.extend(f"{table}:{item['entity_key']}" for item in records.get(table, []))
    return refs


def _runtime_paths(args: argparse.Namespace) -> RuntimePaths:
    if args.config_root:
        os.environ["PEOPLE_TRACKING_FEISHU_CONFIG_ROOT"] = str(args.config_root)
    if args.state_root:
        os.environ["PEOPLE_TRACKING_FEISHU_STATE_ROOT"] = str(args.state_root)
    paths = RuntimePaths.discover()
    paths.ensure()
    return paths


def _lark(executable: str | None, *, strict: bool = True) -> LarkCli | None:
    try:
        cli = LarkCli(executable)
        if strict:
            cli.assert_pinned()
        return cli
    except LarkCliError:
        if strict:
            raise
        return None


def _runtime_mode(config: dict[str, Any] | None) -> str:
    requested = str(((config or {}).get("runtime") or {}).get("mode") or "auto")
    if requested in {"openclaw", "claude_lark_cli"}:
        return requested
    if shutil.which("openclaw") or Path("~/.openclaw").expanduser().exists():
        return "openclaw"
    return "claude_lark_cli"


def _public_auth(status: dict[str, Any]) -> dict[str, Any]:
    identities = status.get("identities") if isinstance(status, dict) else {}
    return {
        "identity": status.get("identity") if isinstance(status, dict) else None,
        "bot_available": bool(((identities or {}).get("bot") or {}).get("available")),
        "user_available": bool(((identities or {}).get("user") or {}).get("available")),
        "brand": status.get("brand") if isinstance(status, dict) else None,
        "secret_values_read": False,
    }


def command_doctor(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths, required=False)
    executable = args.lark_cli or shutil.which("lark-cli")
    lark_report: dict[str, Any] = {
        "executable": executable,
        "required_version": PINNED_LARK_CLI,
        "available": False,
        "version_matches": False,
    }
    if executable:
        try:
            cli = LarkCli(executable)
            version = cli.version()
            lark_report.update(
                {
                    "available": True,
                    "version": version,
                    "version_matches": version == PINNED_LARK_CLI,
                    "doctor": cli.doctor(offline=True),
                    "auth": _public_auth(cli.auth_status()),
                }
            )
        except Exception as exc:
            lark_report["error"] = f"{type(exc).__name__}: {exc}"
    state = PortableState(paths.database)
    try:
        database = state.status()
    finally:
        state.close()
    report = {
        "version": __version__,
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "supported": sys.version_info[:2] in {(3, 11), (3, 12)},
        },
        "node": _tool_version("node", ["--version"]),
        "openclaw": _tool_version("openclaw", ["--version"]),
        "claude": _tool_version("claude", ["--version"]),
        "runtime_mode": _runtime_mode(config),
        "lark_cli": lark_report,
        "configuration": {
            "exists": config is not None,
            "state": config.get("state") if config else None,
            "path": str(paths.config_file),
            "mode_0600": paths.config_file.exists()
            and not bool(paths.config_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)),
        },
        "state": database,
        "mutated_external_configuration": False,
        "secrets_read": False,
    }
    if args.deepseek_smoke:
        if args.confirmation != DEEPSEEK_PHRASE:
            raise ValueError(f"DeepSeek smoke requires --confirmation {DEEPSEEK_PHRASE}")
        if not config:
            raise ValueError("onboarding config is required for DeepSeek smoke")
        report["deepseek_smoke"] = _deepseek_smoke(paths, config)
    elif config:
        try:
            report["deepseek"] = deepseek_public_status(paths, config)
        except Exception as exc:
            report["deepseek"] = {"enabled": True, "configured": False, "error": str(exc)}
    return report


def _tool_version(name: str, args: list[str]) -> dict[str, Any]:
    executable = shutil.which(name)
    if not executable:
        return {"available": False}
    try:
        result = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=10, check=False
        )
        return {
            "available": result.returncode == 0,
            "executable": executable,
            "version_output": (result.stdout or result.stderr).strip()[:500],
        }
    except Exception as exc:
        return {"available": False, "executable": executable, "error": str(exc)}


def _deepseek_smoke(paths: RuntimePaths, config: dict[str, Any]) -> dict[str, Any]:
    config_file = materialize_deepseek_config(paths, config)
    if not config_file:
        raise ValueError("DeepSeek is disabled in onboarding configuration")
    reviewer = DeepSeekDiffReviewer.from_runtime(config_file, required=True)
    assert reviewer
    before = reviewer.usage_snapshot()
    decision = ChangeDecision(
        status="ambiguous",
        score=0.72,
        summary="合成 smoke 差异",
        additions=[
            {
                "key": "synthetic-evidence-1",
                "category": "publication",
                "text": "Synthetic smoke publication, 2026",
            }
        ],
        quality_score=0.9,
    )
    reviewed = reviewer.review(
        decision,
        context={
            "person_key": "synthetic-smoke-person",
            "source_id": "synthetic-smoke-source",
            "source_kind": "homepage",
            "retrieval_mode": "direct",
            "health_status": "healthy",
            "identity_gate_passed": True,
            "quality_score": 0.9,
            "has_confirmed_baseline": True,
        },
    )
    after = reviewer.usage_snapshot()
    result = {
        "passed": reviewed.reviewer.startswith("deepseek:deepseek-v4-flash:"),
        "model": "deepseek-v4-flash",
        "decision_authority": "advisory_only",
        "key_value_returned": False,
        "usage_delta": {
            key: max(0, int(after.get(key, 0)) - int(before.get(key, 0))) for key in after
        },
        "completed_at": utc_now(),
    }
    if not result["passed"]:
        raise RuntimeError("DeepSeek smoke did not use the pinned model")
    state = PortableState(paths.database)
    try:
        state.set_meta("deepseek_smoke", result)
    finally:
        state.close()
    return result


def command_onboarding(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    if args.answers:
        answers = _json_file(args.answers)
        config = normalize_answers(answers)
        backup = None
        if paths.config_file.exists():
            backup = paths.config_file.with_name(
                f"config.backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
            )
            backup.write_bytes(paths.config_file.read_bytes())
            backup.chmod(0o600)
        secure_write_json(paths.config_file, config)
        return {
            "state": "draft",
            "config": _public_config(config),
            "backup": str(backup) if backup else None,
            "next": "run source-probe; do not sync or schedule yet",
        }
    config = load_config(paths, required=False)
    if args.enable:
        if args.confirmation != ENABLE_PHRASE:
            raise ValueError(f"enable requires --confirmation {ENABLE_PHRASE}")
        if not config or config.get("state") != "validated":
            raise ValueError("configuration must be validated by source-probe first")
        deepseek = ((config.get("apis") or {}).get("deepseek") or {})
        state = PortableState(paths.database)
        try:
            if deepseek.get("enabled") and not (state.get_meta("deepseek_smoke") or {}).get("passed"):
                raise ValueError("enabled DeepSeek requires a successful real V4 Flash smoke")
        finally:
            state.close()
        scheduler = _install_configured_scheduler(config, args) if args.register_schedule else None
        enabled = mark_state(paths, config, "enabled")
        return {
            "state": "enabled",
            "config": _public_config(enabled),
            "scheduler": scheduler,
            "next": "run sync --apply, then scan; preview sandbox-e2e before its apply step",
        }
    return {
        "state": config.get("state") if config else "not_started",
        "questionnaire": QUESTIONNAIRE,
        "config": _public_config(config) if config and args.show else None,
        "required_confirmation": ENABLE_PHRASE,
    }


def _public_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    public = json.loads(json.dumps(config))
    deepseek = ((public.get("apis") or {}).get("deepseek") or {})
    if deepseek.get("key_reference"):
        deepseek["key_reference"] = "<configured-reference>"
    for item in ((public.get("apis") or {}).get("search") or []):
        if item.get("secret_reference"):
            item["secret_reference"] = "<configured-reference>"
    return public


def _install_configured_scheduler(
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    launcher = Path(
        getattr(args, "launcher", None)
        or shutil.which("people-tracking-feishu")
        or sys.argv[0]
    ).expanduser().resolve()
    if _runtime_mode(config) == "openclaw":
        return _register_openclaw_scheduler(
            launcher=launcher,
            timezone_name=config["schedule"]["timezone"],
            apply=True,
        )
    return install_local_scheduler(
        home=Path.home(),
        launcher=launcher,
        platform=sys.platform,
        apply=True,
    )


def command_source_probe(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if config.get("state") == "enabled":
        raise ValueError("enabled configuration cannot be silently revalidated; create a new draft")
    if args.bridge_results:
        results = _json_file(args.bridge_results)
        sources = results.get("sources") if isinstance(results, dict) else None
        if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
            raise ValueError("bridge results must contain sources[]")
        if any(item.get("ok") is not True for item in sources):
            raise ValueError("every bridged source probe must state ok=true")
        _validate_bridge_result(
            paths,
            results,
            purpose="source_probe",
            completed_refs=[str(item.get("source_ref") or "") for item in sources],
        )
        validation = {
            "validated_at": utc_now(),
            "adapter": "agent_tool_bridge",
            "results": sources,
            "coverage": {
                "expected_refs": results["expected_refs"],
                "expected_refs_hash": results["expected_refs_hash"],
                "request_hash": results["request_hash"],
            },
            "all_ok": True,
            "secret_values_read": False,
        }
        validated = mark_state(paths, config, "validated", validation=validation)
        return {"state": "validated", "validation": validation, "config": _public_config(validated)}
    mode = _runtime_mode(config)
    cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
    results: list[dict[str, Any]] = []
    bridge_actions: list[dict[str, Any]] = []
    for index, source in enumerate(config["sources"]):
        kind = source["kind"]
        location = source.get("url") or source.get("path")
        ref = f"source-{index + 1}"
        if kind == "local_file":
            records = records_from_file(Path(str(location)).expanduser())
            field_names = sorted({str(key) for record in records[:50] for key in record})
            results.append({"source_ref": ref, "kind": kind, "ok": True, "records": len(records), "fields": field_names})
        elif kind == "people_intel_api":
            results.append(_probe_api(ref, str(location)))
        elif cli is None:
            bridge_actions.append(
                {
                    "source_ref": ref,
                    "kind": kind,
                    "operation": "read_schema_or_document",
                    "url": location,
                    "identity": "user",
                    "read_only": True,
                }
            )
        elif kind == "feishu_base":
            schema = cli.base_schema(
                str(location),
                identity="user",
                configured_table=source.get("table_id") or source.get("table_name"),
            )
            fields = [str(item.get("field_name") or item.get("name") or "") for item in schema["fields"]]
            results.append(
                {
                    "source_ref": ref,
                    "kind": kind,
                    "ok": True,
                    "base_token": schema["base_token"],
                    "selected_table": schema["selected_table"],
                    "fields": fields,
                }
            )
        else:
            document = cli.fetch_document(str(location), identity="user")
            results.append(
                {
                    "source_ref": ref,
                    "kind": kind,
                    "ok": True,
                    "title": first_value(document, "title", "name"),
                    "content_available": bool(
                        first_value(document, "markdown", "content", "text")
                    ),
                }
            )
    master = config.get("master_database") or {}
    if master.get("mode") == "existing_base" and master.get("url"):
        if cli is None:
            bridge_actions.append(
                {
                    "source_ref": "master-database",
                    "kind": "feishu_base",
                    "operation": "read_schema",
                    "url": master["url"],
                    "identity": "user",
                    "read_only": True,
                }
            )
        else:
            schema = cli.base_schema(
                str(master["url"]),
                identity="user",
                configured_table=master.get("people_table_id") or master.get("people_table_name"),
            )
            tables_by_name = {
                str(item.get("name") or ""): str(item.get("table_id") or item.get("id") or "")
                for item in schema["tables"]
            }
            selected = schema.get("selected_table") or {}
            master["base_token"] = schema["base_token"]
            master["people_table_id"] = str(
                selected.get("table_id")
                or selected.get("id")
                or tables_by_name.get(str(master.get("people_table_name") or "People"), "")
            )
            master["sources_table_id"] = str(
                master.get("sources_table_id")
                or tables_by_name.get(str(master.get("sources_table_name") or "Sources"), "")
            )
            config["master_database"] = master
            results.append(
                {
                    "source_ref": "master-database",
                    "kind": "feishu_base",
                    "ok": True,
                    "base_token": schema["base_token"],
                    "tables": schema["tables"],
                    "fields": [
                        str(item.get("field_name") or item.get("name") or "")
                        for item in schema["fields"]
                    ],
                }
            )
    if bridge_actions:
        request = _new_bridge_request(
            paths,
            purpose="source_probe",
            expected_refs=[str(action["source_ref"]) for action in bridge_actions],
            request_content={"actions": bridge_actions, "config": config},
        )
        return {
            "state": "draft",
            "adapter": "agent_tool_bridge",
            "results": results,
            "actions": _bridge_actions(bridge_actions, request),
            "bridge_request": _bridge_public_fields(request),
            "next": "execute read-only actions with the official OpenClaw Feishu plugin and submit --bridge-results",
        }
    if not all(item.get("ok") for item in results):
        return {"state": "draft", "results": results, "all_ok": False}
    validation = {
        "validated_at": utc_now(),
        "adapter": "lark_cli",
        "results": results,
        "all_ok": True,
        "secret_values_read": False,
    }
    validated = mark_state(paths, config, "validated", validation=validation)
    return {"state": "validated", "validation": validation, "config": _public_config(validated)}


def _probe_api(source_ref: str, url: str) -> dict[str, Any]:
    try:
        request = urllib.request.Request(
            url.rstrip("/") + "/health",
            headers={"Accept": "application/json"},
        )
        with _public_urlopen(request, timeout=10) as response:
            body = response.read(1_000_000)
        payload = json.loads(body) if body else {}
        return {"source_ref": source_ref, "kind": "people_intel_api", "ok": response.status == 200, "health": payload}
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"source_ref": source_ref, "kind": "people_intel_api", "ok": False, "error": str(exc)}


def command_sync(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if config.get("state") != "enabled" and not (
        getattr(args, "allow_validated", False) and config.get("state") == "validated"
    ):
        raise ValueError("sync requires enabled configuration")
    mode = _runtime_mode(config)
    cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
    if args.master_bridge_results:
        payload = _json_file(args.master_bridge_results)
        completed_refs = payload.get("completed_refs") if isinstance(payload, dict) else None
        if not isinstance(completed_refs, list):
            raise ValueError("master bridge results must contain completed_refs[]")
        table_ids = payload.get("table_ids") or {}
        if not all(str(table_ids.get(name) or "").strip() for name in ("People", "Sources")):
            raise ValueError("master bridge results require People and Sources table IDs")
        if not str(payload.get("base_token") or "").strip():
            raise ValueError("master bridge results require base_token")
        master = dict(config.get("master_database") or {})
        master.update(
            {
                "mode": "existing_base",
                "base_token": payload.get("base_token"),
                "url": payload.get("base_url"),
                "people_table_id": (payload.get("table_ids") or {}).get("People"),
                "sources_table_id": (payload.get("table_ids") or {}).get("Sources"),
                "created_by_bridge_at": utc_now(),
            }
        )
        config["master_database"] = master
        config["updated_at"] = utc_now()
        validate_config(config, require_complete=True)
        _validate_bridge_result(
            paths,
            payload,
            purpose="master_sync",
            completed_refs=[str(ref) for ref in completed_refs],
        )
        secure_write_json(paths.config_file, config)
        return {"master_bridge_result_applied": True, "master_database": master}
    raw_batches: list[tuple[str, list[dict[str, Any]]]] = []
    if args.bridge_input:
        payload = _json_file(args.bridge_input)
        batches = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(batches, list):
            raise ValueError("bridge input must contain sources[]")
        if any(not isinstance(batch, dict) for batch in batches):
            raise ValueError("bridge input sources must be objects")
        parsed_batches = [
            (
                str(batch.get("source_ref") or ""),
                records_from_payload(batch.get("payload")),
            )
            for batch in batches
        ]
        empty_payload_refs = [
            ref
            for (ref, records), batch in zip(parsed_batches, batches, strict=True)
            if not records and batch.get("payload") not in ([], {"records": []})
        ]
        if empty_payload_refs:
            raise ValueError(
                "bridge source payload did not match the supported records schema: "
                f"refs={empty_payload_refs}; use sources[].payload.records[]"
            )
        internal_empty = not batches and not getattr(args, "include_local_sources", True)
        if not internal_empty:
            _validate_bridge_result(
                paths,
                payload,
                purpose="source_sync",
                completed_refs=[ref for ref, _ in parsed_batches],
            )
        raw_batches.extend(parsed_batches)
        if getattr(args, "include_local_sources", True):
            bridged_refs = {ref for ref, _ in raw_batches}
            for index, source in enumerate(config["sources"]):
                ref = f"source-{index + 1}"
                if source["kind"] == "local_file" and ref not in bridged_refs:
                    raw_batches.append(
                        (
                            ref,
                            records_from_file(Path(str(source.get("path") or source.get("url"))).expanduser()),
                        )
                    )
    else:
        bridge_actions: list[dict[str, Any]] = []
        for index, source in enumerate(config["sources"]):
            ref = f"source-{index + 1}"
            kind = source["kind"]
            location = str(source.get("url") or source.get("path"))
            if kind == "local_file":
                raw_batches.append((ref, records_from_file(Path(location).expanduser())))
            elif kind == "feishu_base" and cli:
                schema = cli.base_schema(
                    location,
                    configured_table=source.get("table_id") or source.get("table_name"),
                )
                selected = schema["selected_table"] or {}
                table_id = str(selected.get("table_id") or selected.get("id") or selected.get("name") or "")
                records = cli.list_base_records(
                    base_token=schema["base_token"],
                    table_id=table_id,
                    view_id=source.get("view_id") or source.get("view_name"),
                    field_names=list(dict.fromkeys(config["field_mapping"].values())),
                )
                raw_batches.append((ref, records))
            elif kind in {"feishu_doc", "feishu_wiki"} and cli:
                raw_batches.append((ref, records_from_payload(cli.fetch_document(location))))
            elif kind == "people_intel_api":
                bridge_actions.append({"source_ref": ref, "operation": "people_intel_export", "url": location})
            else:
                bridge_actions.append(
                    {
                        "source_ref": ref,
                        "operation": "read_people_source",
                        "kind": kind,
                        "url": location,
                        "identity": "user",
                    }
                )
        if bridge_actions:
            request = _new_bridge_request(
                paths,
                purpose="source_sync",
                expected_refs=[str(action["source_ref"]) for action in bridge_actions],
                request_content={"actions": bridge_actions, "config": config},
            )
            return {
                "apply": False,
                "adapter": "agent_tool_bridge",
                "actions": _bridge_actions(bridge_actions, request),
                "bridge_request": _bridge_public_fields(request),
                "local_batches": len(raw_batches),
            }
    canonical_batches = [
        (ref, canonical_records(records, config["field_mapping"])) for ref, records in raw_batches
    ]
    preview = {
        "input_records": sum(len(records) for _, records in canonical_batches),
        "with_required_profile": sum(
            1 for _, records in canonical_batches for record in records if record["canonical_name"] and record["urls"]
        ),
        "missing_required_profile": sum(
            1 for _, records in canonical_batches for record in records if record["canonical_name"] and not record["urls"]
        ),
        "sources": [{"source_ref": ref, "records": len(records)} for ref, records in canonical_batches],
    }
    if not args.apply:
        master = config.get("master_database") or {}
        if master.get("mode") == "create_base":
            preview["master_database"] = (
                create_master_base(
                    cli,
                    timezone_name=config["schedule"]["timezone"],
                    folder_token=master.get("folder_token"),
                    apply=False,
                )
                if cli
                else {
                    "adapter": "agent_tool_bridge",
                    "operation": "create_master_base",
                    "schema": master_schema(),
                }
            )
        return {"apply": False, "preview": preview, "mutated": False}
    state = PortableState(paths.database)
    try:
        imports = [state.import_people(records, source_ref=ref) for ref, records in canonical_batches]
        source_routes = _sync_source_routes(
            state,
            paths,
            list(config.get("source_routes") or []),
            apply=True,
        )
        master_result: dict[str, Any] | None = None
        master = dict(config.get("master_database") or {})
        if master.get("mode") == "create_base" and not master.get("base_token"):
            if cli:
                created = create_master_base(
                    cli,
                    timezone_name=config["schedule"]["timezone"],
                    folder_token=master.get("folder_token"),
                    apply=True,
                )
                master.update(
                    {
                        "mode": "existing_base",
                        "base_token": created["base_token"],
                        "url": created.get("base_url"),
                        "people_table_id": created["table_ids"]["People"],
                        "sources_table_id": created["table_ids"]["Sources"],
                        "created_at": utc_now(),
                    }
                )
                config["master_database"] = master
                config["updated_at"] = utc_now()
                secure_write_json(paths.config_file, config)
                master_result = created
            else:
                bridge_root = paths.state_root / "bridge"
                bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                bridge_file = bridge_root / "master-sync.json"
                records = visible_records(state)
                request = _new_bridge_request(
                    paths,
                    purpose="master_sync",
                    expected_refs=_master_expected_refs(records),
                    request_content={
                        "operation": "create_and_sync_master_base",
                        "master_database": master,
                        "schema": master_schema(),
                        "records": records,
                    },
                    state=state,
                )
                secure_write_json(
                    bridge_file,
                    {
                        "schema": master_schema(),
                        "records": records,
                        **_bridge_public_fields(request),
                        "synthetic": False,
                        "contains_secrets": False,
                    },
                )
                master_result = {
                    "adapter": "agent_tool_bridge",
                    "operation": "create_and_sync_master_base",
                    "payload_file": str(bridge_file),
                    **_bridge_public_fields(request),
                    "next": "submit --master-bridge-results with actual Base/table/record IDs",
                }
        if master.get("mode") == "existing_base":
            if master.get("base_token") and cli:
                people_table = master.get("people_table_id") or master.get("people_table_name") or "People"
                sources_table = master.get("sources_table_id") or master.get("sources_table_name") or "Sources"
                master_result = sync_visible_master(
                    state,
                    cli,
                    base_token=str(master["base_token"]),
                    people_table_id=str(people_table),
                    sources_table_id=str(sources_table),
                    apply=True,
                )
            elif cli is None:
                bridge_root = paths.state_root / "bridge"
                bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                bridge_file = bridge_root / "master-sync.json"
                records = visible_records(state)
                request = _new_bridge_request(
                    paths,
                    purpose="master_sync",
                    expected_refs=_master_expected_refs(records),
                    request_content={
                        "operation": "sync_existing_master_base",
                        "master_database": master,
                        "schema": master_schema(),
                        "records": records,
                    },
                    state=state,
                )
                secure_write_json(
                    bridge_file,
                    {
                        "master_database": master,
                        "schema": master_schema(),
                        "records": records,
                        **_bridge_public_fields(request),
                        "synthetic": False,
                        "contains_secrets": False,
                    },
                )
                master_result = {
                    "adapter": "agent_tool_bridge",
                    "operation": "sync_existing_master_base",
                    "payload_file": str(bridge_file),
                    **_bridge_public_fields(request),
                    "next": "submit --master-bridge-results with actual Base/table IDs",
                }
        status = state.status()
    finally:
        state.close()
    return {
        "apply": True,
        "preview": preview,
        "imports": imports,
        "source_routes": source_routes,
        "master_database": master_result,
        "state": status,
    }


def _sync_source_routes(
    state: PortableState,
    paths: RuntimePaths,
    routes: list[dict[str, Any]],
    *,
    apply: bool,
) -> dict[str, Any]:
    preview = state.sync_source_routes(routes, apply=False)
    if not apply:
        return preview
    if preview["counts"]["blocked"]:
        return state.sync_source_routes(routes, apply=True)
    ready = sum(item.get("status") == "ready" for item in preview["routes"])
    backup = None
    if ready:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = state.backup_database(
            paths.state_root / "backups" / f"source-routes-{stamp}.sqlite3"
        )
    result = state.sync_source_routes(routes, apply=True)
    result["backup"] = backup
    return result


def command_source_routes(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    """Safely preview or apply exact-URL public retrieval route configuration."""

    config = load_config(paths)
    if args.apply and config.get("state") not in {"validated", "enabled"}:
        raise ValueError("source route apply requires validated or enabled configuration")
    state = PortableState(paths.database)
    try:
        return _sync_source_routes(
            state,
            paths,
            list(config.get("source_routes") or []),
            apply=args.apply,
        )
    finally:
        state.close()


def _bootstrap_state(paths: RuntimePaths) -> dict[str, Any]:
    state = PortableState(paths.database)
    try:
        return state.get_meta("bootstrap", {}) or {}
    finally:
        state.close()


def _set_bootstrap_state(paths: RuntimePaths, value: dict[str, Any]) -> None:
    state = PortableState(paths.database)
    try:
        state.set_meta("bootstrap", value)
    finally:
        state.close()


def _anchor_discovery_action(
    paths: RuntimePaths,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    state = PortableState(paths.database)
    try:
        batch_size = int((config.get("intake") or {}).get("discovery_batch_size", 50))
        issues = state.open_anchor_issues(limit=batch_size)
        if not issues:
            return None
        bridge_root = paths.state_root / "bridge"
        bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload_file = bridge_root / "anchor-discovery.json"
        secure_write_json(
            payload_file,
            {
                "schema_version": "people-tracking-anchor-discovery-v1",
                "people": issues,
                "allowed_profile_kinds": ["homepage", "scholar", "github", "linkedin"],
                "rules": {
                    "use_name_secondary_id_alias_school_and_research_context": True,
                    "accept_confirmed_github_nickname_relationship": True,
                    "do_not_require_display_name_exact_match": True,
                    "return_original_profile_and_evidence_urls": True,
                    "do_not_guess_when_identity_is_ambiguous": True,
                },
                "result_schema": {
                    "all_reviewed": True,
                    "resolutions": [
                        {
                            "issue_id": "iss_...",
                            "canonical_name": "same name or confirmed alias",
                            "urls": ["https://profile.example/person"],
                            "evidence_urls": ["https://profile.example/person"],
                            "evidence_notes": "compact identity evidence",
                        }
                    ],
                    "unresolved": [{"issue_id": "iss_...", "reason": "ambiguous"}],
                },
            },
        )
        return {
            "adapter": "agent_tool_bridge",
            "operation": "discover_missing_profile_anchors",
            "payload_file": str(payload_file),
            "people_in_batch": len(issues),
            "read_only_web_research": True,
            "next": "write the result schema to a 0600 JSON file and resume bootstrap with --anchor-bridge-input",
        }
    finally:
        state.close()


def _bootstrap_sync_args(
    args: argparse.Namespace,
    *,
    bridge_input: Path | None = None,
    master_bridge_results: Path | None = None,
    include_local_sources: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        lark_cli=args.lark_cli,
        bridge_input=bridge_input,
        master_bridge_results=master_bridge_results,
        apply=True,
        allow_validated=True,
        include_local_sources=include_local_sources,
    )


def command_bootstrap(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    """Resume-safe first import through baseline and scheduler registration."""

    if args.confirmation != ENABLE_PHRASE:
        raise ValueError(f"bootstrap requires --confirmation {ENABLE_PHRASE}")
    config = load_config(paths)
    if config.get("state") not in {"validated", "enabled"}:
        raise ValueError("bootstrap requires a validated onboarding configuration")
    deepseek = ((config.get("apis") or {}).get("deepseek") or {})
    state = PortableState(paths.database)
    try:
        if deepseek.get("enabled") and not (state.get_meta("deepseek_smoke") or {}).get("passed"):
            raise ValueError("enabled DeepSeek requires a successful real V4 Flash smoke")
    finally:
        state.close()

    progress = _bootstrap_state(paths)
    progress.setdefault("started_at", utc_now())
    progress["status"] = "running"
    progress["updated_at"] = utc_now()
    _set_bootstrap_state(paths, progress)

    if not progress.get("source_import_completed"):
        sync_result = command_sync(
            _bootstrap_sync_args(args, bridge_input=args.source_bridge_input),
            paths,
        )
        if sync_result.get("actions"):
            progress.update({"status": "waiting_for_source_bridge", "updated_at": utc_now()})
            _set_bootstrap_state(paths, progress)
            return {
                "status": progress["status"],
                "actions": sync_result["actions"],
                "next": "execute every read_people_source action and resume bootstrap with --source-bridge-input",
            }
        progress.update(
            {
                "source_import_completed": True,
                "source_import": sync_result.get("preview"),
                "updated_at": utc_now(),
            }
        )
        _set_bootstrap_state(paths, progress)

    intake = config.get("intake") or {}
    if intake.get("missing_anchor_policy", "agent_discovery") == "agent_discovery":
        if args.anchor_bridge_input:
            payload = _json_file(args.anchor_bridge_input)
            state = PortableState(paths.database)
            try:
                progress["anchor_resolution"] = state.apply_anchor_resolutions(
                    payload,
                    minimum_evidence_links=int(intake.get("minimum_evidence_links", 1)),
                )
            finally:
                state.close()
            progress["updated_at"] = utc_now()
            _set_bootstrap_state(paths, progress)
        anchor_action = _anchor_discovery_action(paths, config)
        if anchor_action:
            progress.update({"status": "waiting_for_anchor_discovery", "updated_at": utc_now()})
            _set_bootstrap_state(paths, progress)
            return {
                "status": progress["status"],
                "actions": [anchor_action],
                "next": "research this bounded batch, return evidence-backed results, and resume the same bootstrap command",
            }
    progress["anchor_discovery_completed"] = True
    _set_bootstrap_state(paths, progress)

    if not progress.get("master_database_completed"):
        if args.master_bridge_results:
            master_result = command_sync(
                _bootstrap_sync_args(args, master_bridge_results=args.master_bridge_results),
                paths,
            )
            progress["master_database"] = master_result
            progress["master_database_completed"] = True
            progress["updated_at"] = utc_now()
            _set_bootstrap_state(paths, progress)
        else:
            bridge_root = paths.state_root / "bridge"
            bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            empty_sources = bridge_root / "empty-source-sync.json"
            secure_write_json(empty_sources, {"sources": []})
            master_sync = command_sync(
                _bootstrap_sync_args(
                    args,
                    bridge_input=empty_sources,
                    include_local_sources=False,
                ),
                paths,
            )
            master_result = master_sync.get("master_database")
            if isinstance(master_result, dict) and master_result.get("adapter") == "agent_tool_bridge":
                progress.update(
                    {
                        "status": "waiting_for_master_bridge",
                        "master_action": master_result,
                        "updated_at": utc_now(),
                    }
                )
                _set_bootstrap_state(paths, progress)
                return {
                    "status": progress["status"],
                    "actions": [master_result],
                    "next": "create or sync the visible Base and resume bootstrap with --master-bridge-results",
                }
            progress["master_database"] = master_result
            progress["master_database_completed"] = True
            progress["updated_at"] = utc_now()
            _set_bootstrap_state(paths, progress)

    config = load_config(paths)
    if config.get("state") != "enabled":
        config = mark_state(paths, config, "enabled")
    if not progress.get("baseline_completed"):
        state = PortableState(paths.database)
        try:
            baseline = scan_due(
                state,
                paths,
                config,
                force_all=True,
                force_full_fetch=True,
            )
            progress["baseline"] = {
                "tracker_run_id": baseline["tracker_run_id"],
                "metrics": baseline["metrics"],
                "validation": baseline["validation"],
            }
            if not baseline["validation"]["passed"]:
                progress.update(
                    {
                        "status": "baseline_validation_failed",
                        "updated_at": utc_now(),
                    }
                )
                _set_bootstrap_state(paths, progress)
                return {
                    "status": progress["status"],
                    "baseline": progress["baseline"],
                    "next": (
                        "repair or replace the reported source failures, then "
                        "resume bootstrap; scheduling was not enabled"
                    ),
                }
            progress["baseline_completed"] = True
            progress["updated_at"] = utc_now()
            _set_bootstrap_state(paths, progress)
        finally:
            state.close()

    if not args.skip_schedule and not progress.get("scheduler_registered"):
        scheduler = _install_configured_scheduler(config, args)
        progress["scheduler"] = scheduler
        progress["scheduler_registered"] = True
    state = PortableState(paths.database)
    try:
        final_state = state.status()
    finally:
        state.close()
    progress.update({"status": "ready", "completed_at": utc_now(), "updated_at": utc_now()})
    _set_bootstrap_state(paths, progress)
    return {
        "status": "ready",
        "configuration_state": "enabled",
        "database": final_state,
        "baseline": progress.get("baseline"),
        "scheduler": progress.get("scheduler"),
        "pending_manual_intake": final_state["counts"]["open_intake_issues"],
        "next": "normal schedule is active; future runs use scan and dated daily/weekly digest",
    }


def command_scan(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    state = PortableState(paths.database)
    try:
        return scan_due(
            state,
            paths,
            config,
            force_all=bool(getattr(args, "force_all", False)),
            force_full_fetch=bool(getattr(args, "force_full_fetch", False)),
            source_kinds=getattr(args, "source_kind", None),
            max_error_rate=float(getattr(args, "max_error_rate", 0.10)),
            homepage_retries=int(getattr(args, "homepage_retries", 1)),
            homepage_backoff_seconds=float(
                getattr(args, "homepage_backoff_seconds", 1.0)
            ),
        )
    finally:
        state.close()


def command_digest(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    state = PortableState(paths.database)
    try:
        if args.bridge_results:
            payload = _json_file(args.bridge_results)
            key = str(payload.get("delivery_key") or "")
            if not key or not state.delivery(key):
                raise ValueError("bridge result delivery_key is unknown")
            if payload.get("doc_url"):
                state.update_delivery(key, doc_url=str(payload["doc_url"]), doc_token=str(payload.get("doc_token") or ""))
            if payload.get("message_id"):
                state.update_delivery(key, message_id=str(payload["message_id"]), status="completed")
            elif not ((config.get("outputs") or {}).get("message") or {}).get("enabled"):
                state.update_delivery(key, status="completed")
            return {"bridge_result_applied": True, "delivery": state.delivery(key)}
        mode = _runtime_mode(config)
        cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
        return deliver_digest(
            state,
            paths,
            config,
            output_kind=args.kind,
            tracker_run_id=args.tracker_run_id,
            apply=args.apply,
            lark=cli,
        )
    finally:
        state.close()


def command_sandbox(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if args.apply and args.confirmation != SANDBOX_PHRASE:
        raise ValueError(f"sandbox apply requires --confirmation {SANDBOX_PHRASE}")
    if args.apply and config.get("state") != "enabled":
        raise ValueError("sandbox apply requires enabled configuration")
    sandbox_root = paths.state_root / "sandbox"
    sandbox_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    sandbox_db = sandbox_root / f"sandbox-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    algorithm = algorithm_e2e(sandbox_db)
    mode = _runtime_mode(config)
    cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
    feishu = feishu_e2e(
        lark=cli,
        algorithm=algorithm,
        apply=args.apply,
        message_target=((config.get("outputs") or {}).get("message") or {}),
    )
    return {
        "synthetic_only": True,
        "algorithm": algorithm,
        "feishu": feishu,
        "sandbox_database": str(sandbox_db),
        "production_database_touched": False,
    }


def _register_openclaw_scheduler(
    *,
    launcher: Path,
    timezone_name: str,
    apply: bool,
) -> dict[str, Any]:
    executable = shutil.which("openclaw")
    if not executable:
        raise RuntimeError("OpenClaw runtime selected but openclaw CLI is unavailable")
    listed = subprocess.run(
        [executable, "cron", "list", "--json"], capture_output=True, text=True, timeout=30, check=False
    )
    if listed.returncode != 0:
        raise RuntimeError("unable to inspect existing OpenClaw cron jobs")
    if "people-tracking-feishu-" in listed.stdout:
        raise RuntimeError("a people-tracking-feishu- cron already exists; refusing to overwrite")
    plans = openclaw_cron_plan(launcher, timezone_name)
    if apply:
        for plan in plans:
            argv = [executable, *plan["create_argv"][1:]]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
            if result.returncode != 0:
                raise RuntimeError("OpenClaw cron registration failed")
    return {"apply": apply, "jobs": plans}


def _scan_interval(schedule: dict[str, Any]) -> timedelta:
    cadence = str(schedule.get("scan") or "weekly")
    intervals = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
    }
    try:
        return intervals[cadence]
    except KeyError as exc:
        raise ValueError("schedule.scan must be hourly, daily, or weekly") from exc


def _sync_or_queue_visible_master(
    state: PortableState,
    paths: RuntimePaths,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    master = dict(config.get("master_database") or {})
    if master.get("mode") != "existing_base":
        return None
    base_token = str(master.get("base_token") or "")
    people_table = str(master.get("people_table_id") or master.get("people_table_name") or "")
    sources_table = str(master.get("sources_table_id") or master.get("sources_table_name") or "")
    if not base_token or not people_table or not sources_table:
        raise ValueError("visible master sync requires base_token and People/Sources table IDs")
    if _runtime_mode(config) == "claude_lark_cli":
        cli = _lark(args.lark_cli, strict=True)
        assert cli is not None
        return sync_visible_master(
            state,
            cli,
            base_token=base_token,
            people_table_id=people_table,
            sources_table_id=sources_table,
            apply=True,
        )
    records = visible_records(state)
    request = _new_bridge_request(
        paths,
        purpose="master_sync",
        expected_refs=_master_expected_refs(records),
        request_content={
            "operation": "sync_existing_master_base",
            "master_database": master,
            "schema": master_schema(),
            "records": records,
        },
        state=state,
    )
    bridge_root = paths.state_root / "bridge"
    bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    bridge_file = bridge_root / "scheduled-master-sync.json"
    secure_write_json(
        bridge_file,
        {
            "master_database": master,
            "schema": master_schema(),
            "records": records,
            **_bridge_public_fields(request),
            "synthetic": False,
            "contains_secrets": False,
        },
    )
    return {
        "adapter": "agent_tool_bridge",
        "operation": "sync_existing_master_base",
        "payload_file": str(bridge_file),
        **_bridge_public_fields(request),
        "next": "sync every expected ref and submit --master-bridge-results",
    }


def command_schedule_tick(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if config.get("state") != "enabled":
        return {"skipped": True, "reason": "configuration is not enabled"}
    state = PortableState(paths.database)
    actions: list[dict[str, Any]] = []
    try:
        now = datetime.now(timezone.utc)
        last_scan = state.get_meta("scheduler_last_scan_at")
        scan_due_now = True
        if last_scan:
            parsed_last_scan = datetime.fromisoformat(str(last_scan).replace("Z", "+00:00"))
            if parsed_last_scan.tzinfo is None:
                parsed_last_scan = parsed_last_scan.replace(tzinfo=timezone.utc)
            scan_due_now = now - parsed_last_scan >= _scan_interval(config["schedule"])
        if scan_due_now:
            result = scan_due(state, paths, config)
            actions.append({"operation": "scan", "result": result})
            master_result = _sync_or_queue_visible_master(state, paths, config, args)
            if master_result is not None:
                actions.append({"operation": "sync_visible_master", "result": master_result})
            if not result.get("validation", {}).get("passed", False):
                return {
                    "skipped": False,
                    "validation_failed": True,
                    "actions": actions,
                    "next": "repair or replace failed sources, then rerun the scan",
                }
            state.set_meta("scheduler_last_scan_at", now.isoformat())
        for kind in ("daily", "weekly"):
            if not _digest_due_now(kind, now, config["schedule"]):
                continue
            prepared_period = _scheduler_period(kind, now, config["schedule"]["timezone"])
            if state.get_meta(f"scheduler_digest_{kind}") == prepared_period:
                continue
            mode = _runtime_mode(config)
            cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
            result = deliver_digest(state, paths, config, output_kind=kind, apply=True, lark=cli)
            actions.append({"operation": f"digest_{kind}", "result": result})
            if not result.get("actions") or result.get("delivery", {}).get("status") == "completed":
                state.set_meta(f"scheduler_digest_{kind}", prepared_period)
        return {"skipped": False, "actions": actions}
    finally:
        state.close()


def _scheduler_period(kind: str, now: datetime, timezone_name: str) -> str:
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(timezone_name))
    if kind == "daily":
        return local.date().isoformat()
    start = local.date() - timedelta(days=local.weekday())
    return start.isoformat()


def _digest_due_now(kind: str, now: datetime, schedule: dict[str, Any]) -> bool:
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(schedule["timezone"]))
    if kind == "daily":
        value = str(schedule.get("daily_digest") or "18:00")
        try:
            hour, minute = (int(item) for item in value.split(":", 1))
        except (TypeError, ValueError):
            raise ValueError("daily_digest must use HH:MM")
        return (local.hour, local.minute) >= (hour, minute)
    value = str(schedule.get("weekly_digest") or "MON 08:30").upper()
    try:
        day, clock = value.split(None, 1)
        hour, minute = (int(item) for item in clock.split(":", 1))
    except (TypeError, ValueError):
        raise ValueError("weekly_digest must use DDD HH:MM")
    weekdays = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
    if day not in weekdays:
        raise ValueError("weekly_digest weekday is invalid")
    return local.weekday() > weekdays[day] or (
        local.weekday() == weekdays[day] and (local.hour, local.minute) >= (hour, minute)
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="people-tracking-feishu")
    root.add_argument("--config-root", type=Path)
    root.add_argument("--state-root", type=Path)
    root.add_argument("--lark-cli")
    root.add_argument("--json", action="store_true", help="emit the stable JSON envelope")
    commands = root.add_subparsers(dest="command", required=True)

    onboarding = commands.add_parser("onboarding")
    onboarding.add_argument("--answers", type=Path)
    onboarding.add_argument("--show", action="store_true")
    onboarding.add_argument("--enable", action="store_true")
    onboarding.add_argument("--confirmation")
    onboarding.add_argument("--register-schedule", action="store_true")
    onboarding.add_argument("--launcher")

    probe = commands.add_parser("source-probe")
    probe.add_argument("--bridge-results", type=Path)

    sync = commands.add_parser("sync")
    sync.add_argument("--bridge-input", type=Path)
    sync.add_argument("--master-bridge-results", type=Path)
    sync.add_argument("--apply", action="store_true")

    source_routes = commands.add_parser("source-routes")
    source_routes.add_argument("--apply", action="store_true")

    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--confirmation", required=True)
    bootstrap.add_argument("--source-bridge-input", type=Path)
    bootstrap.add_argument("--anchor-bridge-input", type=Path)
    bootstrap.add_argument("--master-bridge-results", type=Path)
    bootstrap.add_argument("--launcher")
    bootstrap.add_argument("--skip-schedule", action="store_true", help=argparse.SUPPRESS)

    scan = commands.add_parser("scan")
    scan.add_argument("--force-all", action="store_true")
    scan.add_argument(
        "--force-full-fetch",
        action="store_true",
        help=(
            "disable ETag/Last-Modified for already selected sources without "
            "changing which sources are scheduled"
        ),
    )
    scan.add_argument(
        "--source-kind",
        action="append",
        choices=("homepage", "scholar", "github", "linkedin"),
        help="scan only this public source kind; repeat to select multiple kinds",
    )
    scan.add_argument(
        "--max-error-rate",
        type=float,
        default=0.10,
        help="mark scan validation failed when source errors exceed this ratio",
    )
    scan.add_argument(
        "--homepage-retries",
        type=int,
        choices=(0, 1),
        default=1,
        help="retry a narrow transient Homepage failure once at most",
    )
    scan.add_argument(
        "--homepage-backoff-seconds",
        type=_nonnegative_finite_float,
        default=1.0,
        help="initial Homepage retry delay before deterministic jitter",
    )

    digest = commands.add_parser("digest")
    digest.add_argument("--kind", choices=("daily", "weekly"), default="daily")
    digest.add_argument("--tracker-run-id")
    digest.add_argument("--apply", action="store_true")
    digest.add_argument("--bridge-results", type=Path)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--deepseek-smoke", action="store_true")
    doctor.add_argument("--confirmation")

    sandbox = commands.add_parser("sandbox-e2e")
    sandbox.add_argument("--apply", action="store_true")
    sandbox.add_argument("--confirmation")

    commands.add_parser("schedule-tick", help=argparse.SUPPRESS)
    return root


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    # Agent examples naturally put the output flag after the subcommand.  Keep
    # one stable parser while accepting that harmless ordering.
    if "--json" in values:
        values = ["--json", *(value for value in values if value != "--json")]
    args = parser().parse_args(values)
    try:
        paths = _runtime_paths(args)
        handlers = {
            "onboarding": command_onboarding,
            "source-probe": command_source_probe,
            "sync": command_sync,
            "source-routes": command_source_routes,
            "bootstrap": command_bootstrap,
            "scan": command_scan,
            "digest": command_digest,
            "doctor": command_doctor,
            "sandbox-e2e": command_sandbox,
            "schedule-tick": command_schedule_tick,
        }
        data = handlers[args.command](args, paths)
        scan_partial = (
            args.command == "scan"
            and data.get("tracker_run_status") == "partial"
        )
        validation_failed = (
            args.command == "scan"
            and not data.get("validation", {}).get("passed", False)
        ) or (
            args.command == "bootstrap"
            and data.get("status") == "baseline_validation_failed"
        ) or (
            args.command == "schedule-tick"
            and bool(data.get("validation_failed"))
        )
        if validation_failed or scan_partial:
            error_type = "ScanPartialFailure" if scan_partial else "ScanValidationFailed"
            error_message = (
                "scan completed partially; source errors were preserved for retry"
                if scan_partial
                else "scan evidence was preserved, but strict validation failed"
            )
            payload = {
                "ok": False,
                "command": args.command,
                "version": __version__,
                "data": data,
                "error": {
                    "type": error_type,
                    "message": error_message,
                },
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 3
        payload = {"ok": True, "command": args.command, "version": __version__, "data": data}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (
        ConfigError,
        DeepSeekPolicyError,
        LarkCliError,
        RuntimeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        payload = {
            "ok": False,
            "command": getattr(args, "command", None),
            "version": __version__,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
