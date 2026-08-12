from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from zoneinfo import ZoneInfo

from people_intel.deepseek_fallback import (
    DeepSeekDiffReviewer,
    DeepSeekPolicyError,
    DeepSeekSettings,
)
from people_intel.light_cli import _fetch
from people_intel.light_tracker import SOURCE_KINDS

from .config import RuntimePaths, secure_write_json
from .lark import LarkCli, LarkCliError, first_value
from .state import PortableState, stable_hash


_HOMEPAGE_RETRYABLE_STATUSES = {0, 408, 425, 500, 502, 503, 504}
_HOMEPAGE_PERMANENT_ERROR_MARKERS = (
    "authentication wall",
    "auth wall",
    "authwall",
    "captcha",
    "certificate has expired",
    "certificate is not yet valid",
    "certificate verify failed",
    "certificate_verify_failed",
    "hostname mismatch",
    "no shared cipher",
    "no suitable key share",
    "login required",
    "not valid for",
    "sslv3 alert handshake failure",
    "sslv3_alert_handshake_failure",
    "self-signed certificate",
    "self signed certificate",
    "sign in to continue",
    "tlsv1 alert handshake failure",
    "tlsv1 alert insufficient security",
    "tlsv1 alert protocol version",
    "tlsv1_alert_protocol_version",
    "unsupported protocol",
    "unable to get local issuer certificate",
    "unable to verify the first certificate",
    "unsafe legacy renegotiation disabled",
    "wrong version number",
    "wrong_version_number",
)
_HOMEPAGE_RETRY_JITTER_RATIO = 0.25
_HOMEPAGE_RETRY_BACKOFF_SECONDS = 1.0
_ERROR_DECISIONS = {
    "binding_conflict",
    "binding_review",
    "source_issue",
    "source_issue_pending",
}


def _secret_file(reference: str | None) -> Path | None:
    if not reference:
        return None
    value = reference[5:] if reference.startswith("file:") else reference
    if value.startswith(("secret:", "keychain:", "vault:")):
        return None
    return Path(value).expanduser()


def materialize_deepseek_config(paths: RuntimePaths, config: dict[str, Any]) -> Path | None:
    settings = (config.get("apis") or {}).get("deepseek") or {}
    target = paths.config_root / "deepseek.json"
    if not settings.get("enabled"):
        if target.exists():
            target.unlink()
        return None
    key_file = _secret_file(settings.get("key_reference"))
    if key_file is None:
        raise DeepSeekPolicyError(
            "this release needs a mode-0600 key file for unattended DeepSeek use"
        )
    payload = {
        "enabled": True,
        "api_key_file": str(key_file),
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "timeout_seconds": int(settings.get("timeout_seconds", 40)),
        "max_input_chars": int(settings.get("max_input_chars", 12000)),
        "max_output_tokens": int(settings.get("max_output_tokens", 800)),
        "max_calls_per_source_per_day": int(settings.get("max_calls_per_source_per_day", 2)),
        "max_calls_per_person_per_week": int(settings.get("max_calls_per_person_per_week", 10)),
        "max_calls_per_day": int(settings.get("max_calls_per_day", 100)),
        "max_total_tokens_per_day": int(settings.get("max_total_tokens_per_day", 100000)),
        "audit_database": str(paths.state_root / "deepseek-audit.sqlite3"),
    }
    secure_write_json(target, payload)
    return target


def deepseek_public_status(paths: RuntimePaths, config: dict[str, Any]) -> dict[str, Any]:
    deepseek = (config.get("apis") or {}).get("deepseek") or {"enabled": False}
    if not deepseek.get("enabled"):
        return DeepSeekSettings(enabled=False).public_status()
    config_file = materialize_deepseek_config(paths, config)
    assert config_file
    return DeepSeekSettings.from_config_file(config_file).public_status()


def _full_fetch_due(source: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Return whether a selected source needs a real body rather than a 304."""

    last = source.get("last_full_fetch_at")
    if not last:
        return True
    try:
        parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    interval = max(1, int(source.get("full_fetch_interval_days") or 7))
    return parsed <= (now or datetime.now(timezone.utc)) - timedelta(days=interval)


def _prepare_fetches(
    sources: list[dict[str, Any]],
    *,
    force_full_fetch: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Apply body-fetch policy without widening the already selected schedule."""

    prepared = [
        {
            **source,
            "_force_full_fetch": force_full_fetch or _full_fetch_due(source),
        }
        for source in sources
    ]
    return prepared, sum(bool(source["_force_full_fetch"]) for source in prepared)


def _homepage_error_text(observation: Any) -> str:
    parts = [str(observation.error or "")]
    if isinstance(observation.body, bytes):
        parts.append(observation.body[:8_192].decode("utf-8", errors="ignore"))
    elif isinstance(observation.body, str):
        parts.append(observation.body[:8_192])
    return " ".join(parts).casefold()


def _homepage_retryable(observation: Any) -> bool:
    if observation.status_code not in _HOMEPAGE_RETRYABLE_STATUSES:
        return False
    detail = _homepage_error_text(observation)
    return not any(marker in detail for marker in _HOMEPAGE_PERMANENT_ERROR_MARKERS)


def _homepage_retry_delay(
    source: dict[str, Any],
    *,
    retry_count: int,
    base_seconds: float,
) -> float:
    """Return deterministic exponential backoff with stable per-source jitter."""

    exponential = max(0.0, base_seconds) * (2 ** max(0, retry_count))
    identity = f"{source.get('source_id', '')}|{source.get('url', '')}|{retry_count}"
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    unit_interval = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
    return exponential * (1.0 + _HOMEPAGE_RETRY_JITTER_RATIO * unit_interval)


def _fetch_with_policy(
    source: dict[str, Any],
    *,
    homepage_retries: int = 1,
    homepage_backoff_seconds: float = _HOMEPAGE_RETRY_BACKOFF_SECONDS,
) -> tuple[Any, int, float]:
    """Fetch once, with at most one narrow transient retry for Homepages."""

    retries = 0
    waited = 0.0
    maximum_retries = min(1, max(0, int(homepage_retries)))
    while True:
        observation = _fetch(source)
        if (
            source.get("kind") != "homepage"
            or not _homepage_retryable(observation)
            or retries >= maximum_retries
        ):
            return observation, retries, waited
        delay = _homepage_retry_delay(
            source,
            retry_count=retries,
            base_seconds=homepage_backoff_seconds,
        )
        retries += 1
        if delay:
            sleep(delay)
            waited += delay


def _selected_kinds(source_kinds: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    selected = tuple(
        dict.fromkeys(SOURCE_KINDS if source_kinds is None else source_kinds)
    )
    if not selected or any(kind not in SOURCE_KINDS for kind in selected):
        raise ValueError("source_kinds must contain one or more supported public kinds")
    return selected


def _scan_validation(
    outcomes: list[dict[str, Any]],
    *,
    max_error_rate: float,
    force_all: bool,
    sources_enabled: int,
    sources_planned: int,
    sources_attempted: int,
    full_fetch_planned: int,
    full_fetch_attempted: int,
    observation_errors: int = 0,
) -> dict[str, Any]:
    if not 0.0 <= max_error_rate <= 1.0:
        raise ValueError("max_error_rate must be between 0 and 1")
    errors = [
        item
        for item in outcomes
        if item.get("health_status") != "healthy"
        or item.get("decision") in _ERROR_DECISIONS
    ]
    error_count = len(errors) + observation_errors
    error_rate = error_count / sources_attempted if sources_attempted else 0.0
    reasons: list[str] = []
    if observation_errors:
        reasons.append(
            f"{observation_errors} source observation(s) raised internal exceptions"
        )
    if force_all and sources_enabled == 0:
        reasons.append("strict full scan has zero enabled sources")
    if error_rate > max_error_rate:
        reasons.append(
            f"source error rate {error_rate:.2%} exceeds the "
            f"{max_error_rate:.2%} limit"
        )
    if force_all and sources_planned != sources_enabled:
        reasons.append(
            f"full scan planned {sources_planned} of {sources_enabled} enabled sources"
        )
    if sources_attempted != sources_planned:
        reasons.append(
            f"scan attempted {sources_attempted} of {sources_planned} planned sources"
        )
    if len(outcomes) != sources_attempted:
        reasons.append(
            f"scan observed {len(outcomes)} of {sources_attempted} attempted sources"
        )
    if full_fetch_attempted != full_fetch_planned:
        reasons.append(
            "full-body fetch attempted "
            f"{full_fetch_attempted} of {full_fetch_planned} planned sources"
        )
    passed = not reasons
    return {
        "passed": passed,
        "max_error_rate": max_error_rate,
        "error_sources": error_count,
        "observed_sources": len(outcomes),
        "error_rate": round(error_rate, 6),
        "coverage": {
            "force_all": force_all,
            "sources_enabled": sources_enabled,
            "sources_planned": sources_planned,
            "sources_attempted": sources_attempted,
            "sources_observed": len(outcomes),
            "full_fetch_planned": full_fetch_planned,
            "full_fetch_attempted": full_fetch_attempted,
        },
        "reasons": reasons,
    }


def scan_due(
    state: PortableState,
    paths: RuntimePaths,
    config: dict[str, Any],
    *,
    force_all: bool = False,
    force_full_fetch: bool = False,
    source_kinds: list[str] | tuple[str, ...] | None = None,
    max_error_rate: float = 0.10,
    homepage_retries: int = 1,
    homepage_backoff_seconds: float = _HOMEPAGE_RETRY_BACKOFF_SECONDS,
) -> dict[str, Any]:
    if config.get("state") != "enabled":
        raise ValueError("tracking is not enabled; complete onboarding and confirm first")
    selected_kinds = _selected_kinds(source_kinds)
    if not 0.0 <= max_error_rate <= 1.0:
        raise ValueError("max_error_rate must be between 0 and 1")
    retry_limit = min(1, max(0, int(homepage_retries)))
    retry_backoff = float(homepage_backoff_seconds)
    if not math.isfinite(retry_backoff) or retry_backoff < 0:
        raise ValueError("homepage_backoff_seconds must be a finite non-negative number")
    config_file = materialize_deepseek_config(paths, config)
    reviewer = None
    settings = DeepSeekSettings(enabled=False)
    if config_file:
        settings = DeepSeekSettings.from_config_file(config_file)
        reviewer = DeepSeekDiffReviewer.from_runtime(config_file, required=True)
    tracker_run = state.tracker.start_run("feishu-portable-scan")
    runtime_run = state.begin_run("scan")
    started = perf_counter()
    timing: dict[str, float] = {}
    opportunities: dict[str, int] = {}
    usage_before = reviewer.usage_snapshot() if reviewer else {}
    enabled_sources = [
        {**source, "person_key": person["person_key"]}
        for person in state.tracker.list_people()
        for source in person["sources"]
        if int(source.get("tracking_enabled", 1)) == 1
        and source.get("kind") in selected_kinds
    ]
    selected_sources = (
        enabled_sources
        if force_all
        else [
            source
            for source in state.tracker.list_due_sources(kinds=selected_kinds)
            if int(source.get("tracking_enabled", 1)) == 1
        ]
    )
    sources, full_fetch_planned = _prepare_fetches(
        selected_sources,
        force_full_fetch=force_full_fetch,
    )
    outcomes: list[dict[str, Any]] = []
    observation_errors: list[dict[str, Any]] = []
    sources_attempted = 0
    full_fetch_attempted = 0
    homepage_planned = sum(source.get("kind") == "homepage" for source in sources)
    homepage_attempted = 0
    homepage_retries = 0
    homepage_retry_wait_seconds = 0.0
    try:
        for source in sources:
            sources_attempted += 1
            if source.get("_force_full_fetch"):
                full_fetch_attempted += 1
            if source.get("kind") == "homepage":
                homepage_attempted += 1
            fetch_started = perf_counter()
            try:
                observation, retries, waited = _fetch_with_policy(
                    source,
                    homepage_retries=retry_limit,
                    homepage_backoff_seconds=retry_backoff,
                )
                homepage_retries += retries
                homepage_retry_wait_seconds += waited
            except Exception as exc:
                observation_errors.append(
                    {
                        "source_id": source["source_id"],
                        "kind": source.get("kind"),
                        "phase": "fetch",
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:800],
                    }
                )
                continue
            finally:
                timing["fetch_seconds"] = timing.get("fetch_seconds", 0.0) + (
                    perf_counter() - fetch_started
                )
            try:
                decision = state.tracker.observe(
                    tracker_run,
                    source["source_id"],
                    observation,
                    reviewer=reviewer,
                    timing_sink=timing,
                    ai_usage_sink=opportunities,
                )
            except Exception as exc:
                observation_errors.append(
                    {
                        "source_id": source["source_id"],
                        "kind": source.get("kind"),
                        "phase": "observe",
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:800],
                    }
                )
                continue
            outcomes.append(
                {
                    "source_id": source["source_id"],
                    "person_key": source["person_key"],
                    "kind": source["kind"],
                    "decision": decision.status,
                    "health_status": decision.health_status,
                    "summary": decision.summary,
                }
            )
        tracker_run_status = "partial" if observation_errors else "completed"
        usage_after = reviewer.usage_snapshot() if reviewer else {}
        usage = {
            key: max(0, int(usage_after.get(key, 0)) - int(usage_before.get(key, 0)))
            for key in usage_after
        }
        validation = _scan_validation(
            outcomes,
            max_error_rate=max_error_rate,
            force_all=force_all,
            sources_enabled=len(enabled_sources),
            sources_planned=len(sources),
            sources_attempted=sources_attempted,
            full_fetch_planned=full_fetch_planned,
            full_fetch_attempted=full_fetch_attempted,
            observation_errors=len(observation_errors),
        )
        metrics = {
            "tracker_run_id": tracker_run,
            "tracker_run_status": tracker_run_status,
            "observation_errors": observation_errors,
            "sources_due": len(sources),
            "sources_enabled": len(enabled_sources),
            "sources_planned": len(sources),
            "sources_attempted": sources_attempted,
            "force_all": force_all,
            "force_full_fetch": force_full_fetch,
            "source_kinds": list(selected_kinds),
            "full_fetch_planned": full_fetch_planned,
            "full_fetch_attempted": full_fetch_attempted,
            "homepage_planned": homepage_planned,
            "homepage_attempted": homepage_attempted,
            "homepage_retry_limit": retry_limit,
            "homepage_backoff_seconds": retry_backoff,
            "homepage_retries": homepage_retries,
            "homepage_retry_wait_seconds": round(homepage_retry_wait_seconds, 6),
            "validation": validation,
            "decisions": _counts(item["decision"] for item in outcomes),
            "health": _counts(item["health_status"] for item in outcomes),
            "timing_seconds": {
                **{key: round(value, 6) for key, value in timing.items()},
                "wall": round(perf_counter() - started, 6),
            },
            "deepseek": {
                **settings.public_status(),
                "active_this_run": reviewer is not None,
                "opportunities": opportunities,
                "usage_this_run": usage,
            },
        }
        state.tracker.complete_run(
            tracker_run,
            observation_errors=observation_errors,
        )
        if validation["passed"]:
            state.set_meta("last_tracker_run_id", tracker_run)
        state.complete_run(runtime_run, metrics)
        return {
            "run_id": runtime_run,
            "tracker_run_id": tracker_run,
            "tracker_run_status": tracker_run_status,
            "metrics": metrics,
            "validation": validation,
            "reasons": validation["reasons"],
            "next": (
                None
                if validation["passed"]
                else "repair or replace failed sources, then rerun scan validation"
            ),
            "outcomes": outcomes,
        }
    except Exception as exc:
        state.tracker.fail_run(
            tracker_run,
            exc,
            observation_errors=observation_errors,
        )
        state.complete_run(
            runtime_run,
            {"tracker_run_id": tracker_run, "error_type": type(exc).__name__},
            status="failed",
        )
        raise


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _period(output_kind: str, timezone_name: str, at: datetime | None = None) -> tuple[str, str]:
    zone = ZoneInfo(timezone_name)
    now = (at or datetime.now(zone)).astimezone(zone)
    if output_kind == "daily":
        value = now.date().isoformat()
        return value, f"人才跟踪日报｜{value}"
    if output_kind != "weekly":
        raise ValueError("output_kind must be daily or weekly")
    start = now.date() - timedelta(days=now.weekday())
    end = start + timedelta(days=6)
    value = f"{start.isoformat()}—{end.isoformat()}"
    return value, f"人才跟踪周报｜{value}"


def _report_stats(state: PortableState, tracker_run_id: str) -> dict[str, Any]:
    rows = state.db.execute(
        """SELECT o.decision_status,o.source_id,s.person_key,s.kind,s.url,p.canonical_name,
                  o.summary,o.delta_json
           FROM observations o JOIN sources s ON s.source_id=o.source_id
           JOIN people p ON p.person_key=s.person_key WHERE o.run_id=?
           ORDER BY p.canonical_name,s.kind""",
        (tracker_run_id,),
    ).fetchall()
    changed = [dict(row) for row in rows if row["decision_status"] == "changed"]
    issues = [
        dict(row)
        for row in rows
        if row["decision_status"] in {"source_issue", "source_issue_pending"}
    ]
    return {
        "observed_sources": len(rows),
        "changed_sources": len(changed),
        "changed_people": len({row["person_key"] for row in changed}),
        "source_issues": len(issues),
        "changed": changed,
    }


def _message_markdown(stats: dict[str, Any], doc_url: str | None) -> str:
    lines = [
        "**人才跟踪更新**",
        "",
        f"本轮发现 {stats['changed_people']} 位人才、{stats['changed_sources']} 个来源有已确认变化；来源异常 {stats['source_issues']} 个。",
    ]
    for row in stats["changed"][:10]:
        lines.append(f"- {row['canonical_name']}（{row['kind']}）：{row['summary']} [原始链接]({row['url']})")
    if doc_url:
        lines.extend(["", f"[查看完整日期报告]({doc_url})"])
    message = "\n".join(lines)
    return message[:2950] + ("…" if len(message) > 2950 else "")


def prepare_digest(
    state: PortableState,
    paths: RuntimePaths,
    config: dict[str, Any],
    *,
    output_kind: str,
    tracker_run_id: str | None = None,
) -> dict[str, Any]:
    if config.get("state") != "enabled":
        raise ValueError("tracking is not enabled")
    tracker_run_id = tracker_run_id or state.get_meta("last_tracker_run_id")
    if not tracker_run_id:
        raise ValueError("no completed tracking scan is available")
    period, title = _period(output_kind, config["schedule"]["timezone"])
    delivery_key = f"{tracker_run_id}:{output_kind}:{period}"
    report = state.tracker.render_report(tracker_run_id)
    report_path = paths.reports / f"{delivery_key.replace(':', '-')}.md"
    report_path.write_text(report, encoding="utf-8")
    report_path.chmod(0o600)
    delivery = state.prepare_delivery(
        key=delivery_key,
        output_kind=output_kind,
        period=period,
        title=title,
        report_path=str(report_path),
    )
    return {
        "delivery_key": delivery_key,
        "title": title,
        "period": period,
        "report": report,
        "report_path": str(report_path),
        "delivery": delivery,
        "stats": _report_stats(state, tracker_run_id),
    }


def deliver_digest(
    state: PortableState,
    paths: RuntimePaths,
    config: dict[str, Any],
    *,
    output_kind: str,
    tracker_run_id: str | None = None,
    apply: bool,
    lark: LarkCli | None,
) -> dict[str, Any]:
    prepared = prepare_digest(
        state,
        paths,
        config,
        output_kind=output_kind,
        tracker_run_id=tracker_run_id,
    )
    key = prepared["delivery_key"]
    existing = state.delivery(key) or {}
    if existing.get("status") == "completed":
        return {**prepared, "idempotent_replay": True, "actions": []}
    actions: list[dict[str, Any]] = []
    outputs = config.get("outputs") or {}
    document = outputs.get("document") or {"enabled": False}
    message = outputs.get("message") or {"enabled": False}
    doc_url = existing.get("doc_url")
    if document.get("enabled") and not doc_url:
        if lark is None:
            actions.append(
                {
                    "adapter": "agent_tool_bridge",
                    "operation": "create_document",
                    "title": prepared["title"],
                    "markdown_file": prepared["report_path"],
                    "identity": "bot",
                    "idempotency_key": f"{key}:doc",
                    "target": document,
                }
            )
        else:
            preview, actual = lark.create_document(
                title=prepared["title"],
                markdown=prepared["report"],
                identity="bot",
                folder_token=document.get("folder_token"),
                wiki_space=document.get("wiki_space"),
                wiki_node=document.get("wiki_node"),
                apply=apply,
            )
            actions.append({"operation": "create_document", "preview": preview.public()})
            if actual:
                doc_url = first_value(actual.payload, "url", "document_url", "wiki_url")
                doc_token = first_value(actual.payload, "document_id", "document_token", "token")
                if not doc_url:
                    raise LarkCliError("document create response did not include a URL")
                state.update_delivery(key, doc_url=str(doc_url), doc_token=str(doc_token or ""), status="document_created")
    message_text = _message_markdown(prepared["stats"], str(doc_url) if doc_url else None)
    if message.get("enabled"):
        target_kind = str(message.get("target_kind") or "current_chat")
        target_id = str(message.get("target_id") or "")
        if lark is None or target_kind == "current_chat":
            actions.append(
                {
                    "adapter": "agent_tool_bridge",
                    "operation": "send_current_or_configured_message",
                    "markdown": message_text,
                    "identity": "bot",
                    "idempotency_key": f"{key}:message",
                    "target": message,
                }
            )
        else:
            preview, actual = lark.send_message(
                markdown=message_text,
                idempotency_key=f"{key}:message",
                target_kind=target_kind,
                target_id=target_id,
                identity="bot",
                apply=apply,
            )
            actions.append({"operation": "send_message", "preview": preview.public()})
            if actual:
                message_id = first_value(actual.payload, "message_id", "messageId", "id")
                if not message_id:
                    raise LarkCliError("message response did not include message_id")
                state.update_delivery(key, message_id=str(message_id), status="completed")
    if not message.get("enabled") and (doc_url or not document.get("enabled")) and apply:
        state.update_delivery(key, status="completed")
    return {
        **prepared,
        "idempotent_replay": False,
        "apply": apply,
        "document_url": doc_url,
        "message_markdown": message_text,
        "actions": actions,
        "delivery": state.delivery(key),
    }
