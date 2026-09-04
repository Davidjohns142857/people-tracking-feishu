from __future__ import annotations

import hashlib
import json
import math
import re
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from zoneinfo import ZoneInfo

from people_intel.light_cli import _fetch
from people_intel.light_tracker import SOURCE_KINDS

from .config import RuntimePaths
from .lark import LarkCli, LarkCliError, first_value
from .reporting import (
    apply_agent_review_decisions,
    markdown_http_url,
    materialize_window_events,
    public_event_stats,
    render_developer_report,
    render_public_message,
    render_public_report,
    report_content_hash,
    resolve_report_window,
    window_dict,
    write_agent_review_bundle,
)
from .state import PortableState


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
    "parser_anomaly",
}
_SCHOLAR_HOST_KEY = "scholar.google"


def _assert_execution_agent_review_policy(config: dict[str, Any]) -> None:
    """Fail closed if a caller bypasses normal configuration validation.

    The unattended scanner only produces deterministic evidence.  Publication
    decisions belong to the Agent executing the Skill and never to a separately
    configured model runtime.
    """

    review = config.get("agent_review") or {}
    if review.get("mode", "execution_agent") != "execution_agent":
        raise ValueError("agent_review.mode must be execution_agent")
    if review.get("required_for_publish", True) is not True:
        raise ValueError("agent_review.required_for_publish must remain true")
    legacy_review = ((config.get("apis") or {}).get("deepseek") or {})
    if legacy_review.get("enabled"):
        raise ValueError(
            "external runtime review was removed in v0.9; use the execution Agent"
        )


def _agent_review_batch_size(config: dict[str, Any]) -> int:
    value = int((config.get("agent_review") or {}).get("batch_size", 100))
    if value not in range(1, 201):
        raise ValueError("agent_review.batch_size must be between 1 and 200")
    return value


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


def _parse_iso_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _scholar_policy(config: dict[str, Any]) -> dict[str, int]:
    configured = ((config.get("scan_policy") or {}).get("scholar") or {})
    return {
        "max_requests_per_run": max(1, min(100, int(configured.get("max_requests_per_run", 8)))),
        "max_requests_per_day": max(
            1, min(500, int(configured.get("max_requests_per_day", 64)))
        ),
        "max_requests_per_week": max(
            1, min(2000, int(configured.get("max_requests_per_week", 448)))
        ),
        "recovery_canary_requests": max(
            1, min(10, int(configured.get("recovery_canary_requests", 1)))
        ),
        "default_retry_after_hours": max(
            1, min(168, int(configured.get("default_retry_after_hours", 24)))
        ),
        "maximum_retry_after_hours": max(
            1, min(336, int(configured.get("maximum_retry_after_hours", 168)))
        ),
    }


def _retry_after_seconds(
    observation: Any,
    *,
    now: datetime,
    fallback_seconds: int,
    maximum_seconds: int,
) -> int:
    value = ""
    match = re.search(r"(?:^|;\s*)retry_after=([^;]+)", str(observation.error or ""), re.I)
    if match:
        value = match.group(1).strip()
    seconds = fallback_seconds
    if value.isdigit():
        seconds = max(1, int(value))
    elif value:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = max(1, int((retry_at.astimezone(timezone.utc) - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    return min(maximum_seconds, seconds)


def _apply_scholar_run_budget(
    state: PortableState,
    sources: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply a process-persistent host circuit and a bounded per-run budget."""

    policy = _scholar_policy(config)
    existing = state.get_host_budget(_SCHOLAR_HOST_KEY)
    circuit_state = str((existing or {}).get("circuit_state") or "closed")
    blocked_until = _parse_iso_time((existing or {}).get("blocked_until"))
    recovery = bool((existing or {}).get("canary_required"))
    metadata = dict((existing or {}).get("metadata") or {})
    history = [
        parsed
        for value in metadata.get("request_timestamps") or []
        if (parsed := _parse_iso_time(value)) is not None
        and parsed > now - timedelta(days=7)
        and parsed <= now
    ]
    used_day = sum(value > now - timedelta(days=1) for value in history)
    used_week = len(history)
    quota_allowance = min(
        policy["max_requests_per_day"] - used_day,
        policy["max_requests_per_week"] - used_week,
    )
    if circuit_state == "open" and blocked_until and blocked_until > now:
        allowance = 0
        mode = "blocked"
    elif circuit_state in {"open", "half_open"} or recovery:
        allowance = min(
            policy["recovery_canary_requests"],
            policy["max_requests_per_run"],
            quota_allowance,
        )
        mode = "half_open"
        state.update_host_budget(
            _SCHOLAR_HOST_KEY,
            circuit_state="half_open",
            blocked_until=None,
            canary_required=True,
            reason="retry_window_elapsed_canary",
        )
    else:
        allowance = min(policy["max_requests_per_run"], quota_allowance)
        mode = "closed"
    allowance = max(0, allowance)
    selected: list[dict[str, Any]] = []
    scholar_seen = 0
    scholar_selected = 0
    for source in sources:
        if source.get("kind") != "scholar":
            selected.append(source)
            continue
        scholar_seen += 1
        if scholar_selected < allowance:
            selected.append(source)
            scholar_selected += 1
    return selected, {
        "host_key": _SCHOLAR_HOST_KEY,
        "mode": mode,
        "policy": policy,
        "circuit_before": existing,
        "due": scholar_seen,
        "planned": scholar_selected,
        "deferred_before_fetch": scholar_seen - scholar_selected,
        "requests_used_last_24h": used_day,
        "requests_used_last_7d": used_week,
        "quota_allowance_before_run": max(0, quota_allowance),
        "attempted": 0,
        "access_successes": 0,
        "deferred_after_limit": 0,
        "stopped_on_429": False,
    }


def _record_scholar_attempt(
    state: PortableState, *, at: datetime
) -> dict[str, Any]:
    existing = state.get_host_budget(_SCHOLAR_HOST_KEY) or {}
    metadata = dict(existing.get("metadata") or {})
    history = [
        parsed.isoformat()
        for value in metadata.get("request_timestamps") or []
        if (parsed := _parse_iso_time(value)) is not None
        and parsed > at - timedelta(days=7)
        and parsed <= at
    ]
    history.append(at.isoformat())
    metadata["request_timestamps"] = history[-2000:]
    metadata["source"] = "google_scholar"
    return state.update_host_budget(
        _SCHOLAR_HOST_KEY,
        last_attempt_at=at.isoformat(),
        metadata=metadata,
    )


def _open_scholar_circuit(
    state: PortableState,
    observation: Any,
    config: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    policy = _scholar_policy(config)
    existing = state.get_host_budget(_SCHOLAR_HOST_KEY) or {}
    limits = int(existing.get("consecutive_limits") or 0) + 1
    default_seconds = min(
        policy["maximum_retry_after_hours"] * 3600,
        policy["default_retry_after_hours"] * 3600 * (2 ** (limits - 1)),
    )
    retry_after = _retry_after_seconds(
        observation,
        now=now,
        fallback_seconds=default_seconds,
        maximum_seconds=policy["maximum_retry_after_hours"] * 3600,
    )
    blocked_until = (now + timedelta(seconds=retry_after)).isoformat()
    metadata = dict(existing.get("metadata") or {})
    metadata.update(
        {"source": "google_scholar", "stopped_remaining_requests": True}
    )
    return state.update_host_budget(
        _SCHOLAR_HOST_KEY,
        circuit_state="open",
        blocked_until=blocked_until,
        reason="http_429",
        status_code=429,
        retry_after_seconds=retry_after,
        consecutive_limits=limits,
        canary_required=True,
        last_attempt_at=now.isoformat(),
        metadata=metadata,
    )


def _reopen_scholar_after_unhealthy_canary(
    state: PortableState,
    observation: Any,
    decision: Any,
    config: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Reopen the circuit when HTTP success did not yield a healthy parse."""

    policy = _scholar_policy(config)
    existing = state.get_host_budget(_SCHOLAR_HOST_KEY) or {}
    failures = int(existing.get("consecutive_limits") or 0) + 1
    retry_after = min(
        policy["maximum_retry_after_hours"] * 3600,
        policy["default_retry_after_hours"] * 3600 * (2 ** (failures - 1)),
    )
    metadata = dict(existing.get("metadata") or {})
    metadata.update(
        {
            "source": "google_scholar",
            "canary_failure": str(decision.status),
            "canary_health_status": str(decision.health_status),
            "stopped_remaining_requests": True,
        }
    )
    return state.update_host_budget(
        _SCHOLAR_HOST_KEY,
        circuit_state="open",
        blocked_until=(now + timedelta(seconds=retry_after)).isoformat(),
        reason=f"unhealthy_canary:{decision.status}",
        status_code=int(observation.status_code or 0),
        retry_after_seconds=retry_after,
        consecutive_limits=failures,
        canary_required=True,
        last_attempt_at=now.isoformat(),
        metadata=metadata,
    )


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
    source_ids: list[str] | tuple[str, ...] | None = None,
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
    # No credential is read and no model/API is instantiated; grey-zone evidence
    # is exported for the Agent executing the Skill at report preparation time.
    _assert_execution_agent_review_policy(config)
    tracker_run = state.tracker.start_run("feishu-portable-scan")
    runtime_run = state.begin_run("scan")
    started = perf_counter()
    timing: dict[str, float] = {}
    opportunities: dict[str, int] = {}
    enabled_sources = [
        {**source, "person_key": person["person_key"]}
        for person in state.tracker.list_people()
        for source in person["sources"]
        if int(source.get("tracking_enabled", 1)) == 1
        and source.get("kind") in selected_kinds
    ]
    explicitly_selected = {str(value) for value in source_ids or [] if str(value)}
    if force_all:
        selected_sources = enabled_sources
    else:
        due = [
            source
            for source in state.tracker.list_due_sources(kinds=selected_kinds)
            if int(source.get("tracking_enabled", 1)) == 1
        ]
        by_id = {str(source["source_id"]): source for source in due}
        if explicitly_selected:
            by_id.update(
                {
                    str(source["source_id"]): source
                    for source in enabled_sources
                    if str(source["source_id"]) in explicitly_selected
                }
            )
        selected_sources = sorted(
            by_id.values(),
            key=lambda item: (
                str(item.get("next_check_at") or ""),
                str(item.get("canonical_name") or ""),
                str(item.get("source_id") or ""),
            ),
        )
    budget_now = datetime.now(timezone.utc)
    selected_sources, scholar_budget = _apply_scholar_run_budget(
        state, selected_sources, config, now=budget_now
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
    scholar_stopped = False
    try:
        for source in sources:
            if source.get("kind") == "scholar" and scholar_stopped:
                scholar_budget["deferred_after_limit"] += 1
                continue
            sources_attempted += 1
            if source.get("_force_full_fetch"):
                full_fetch_attempted += 1
            if source.get("kind") == "homepage":
                homepage_attempted += 1
            if source.get("kind") == "scholar":
                # Count the attempt before transport execution: connection and
                # parser exceptions still consume the host budget and must not
                # make a half-open canary look as if it never ran.
                scholar_budget["attempted"] += 1
                _record_scholar_attempt(state, at=datetime.now(timezone.utc))
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
            if source.get("kind") == "scholar":
                if int(observation.status_code or 0) == 429:
                    scholar_budget["stopped_on_429"] = True
                    scholar_stopped = True
                    scholar_budget["circuit_after"] = _open_scholar_circuit(
                        state,
                        observation,
                        config,
                        now=_parse_iso_time(observation.observed_at) or budget_now,
                    )
            try:
                decision = state.tracker.observe(
                    tracker_run,
                    source["source_id"],
                    observation,
                    reviewer=None,
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
            if source.get("kind") == "scholar":
                status_code = int(observation.status_code or 0)
                # A half-open circuit is a parser/health canary, not merely an
                # HTTP reachability probe.  Redirects, 304s, challenge pages and
                # malformed 200 responses cannot certify a healthy Scholar page.
                if (
                    200 <= status_code < 300
                    and observation.body is not None
                    and decision.health_status == "healthy"
                    and decision.status not in _ERROR_DECISIONS
                    and decision.quality_usable is True
                ):
                    scholar_budget["access_successes"] += 1
                elif scholar_budget["mode"] == "half_open":
                    scholar_stopped = True
                    scholar_budget["circuit_after"] = (
                        _reopen_scholar_after_unhealthy_canary(
                            state,
                            observation,
                            decision,
                            config,
                            now=(
                                _parse_iso_time(observation.observed_at)
                                or datetime.now(timezone.utc)
                            ),
                        )
                    )
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
        if (
            scholar_budget["mode"] == "half_open"
            and scholar_budget["attempted"] > 0
            and scholar_budget["access_successes"] == scholar_budget["attempted"]
            and not scholar_budget["stopped_on_429"]
        ):
            scholar_budget["circuit_after"] = state.reset_host_budget(
                _SCHOLAR_HOST_KEY,
                reason="successful_canary",
                at=budget_now.isoformat(),
            )
        elif "circuit_after" not in scholar_budget:
            scholar_budget["circuit_after"] = state.get_host_budget(_SCHOLAR_HOST_KEY)
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
            "explicit_source_ids": sorted(explicitly_selected),
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
            "agent_review": {
                "mode": "execution_agent",
                "runtime_model_calls": 0,
                "review_opportunities": opportunities,
            },
            "scholar_budget": scholar_budget,
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
    """Legacy calendar bucket helper retained for callers outside the v0.9 ledger."""

    zone = ZoneInfo(timezone_name)
    now = (at or datetime.now(zone)).astimezone(zone)
    if output_kind == "daily":
        value = now.date().isoformat()
        return value, f"人员最新动态日报｜{value}"
    if output_kind != "weekly":
        raise ValueError("output_kind must be daily or weekly")
    iso_year, iso_week, _ = now.date().isocalendar()
    value = f"{iso_year}-W{iso_week:02d}"
    return value, f"人员最新动态周报｜{value}"


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _audience_outputs(config: dict[str, Any], audience: str) -> dict[str, Any]:
    outputs = config.get("outputs") or {}
    if audience == "developer":
        configured = outputs.get("developer") or {}
        return configured if isinstance(configured, dict) else {}
    configured = outputs.get("public")
    if isinstance(configured, dict):
        return configured
    # v0.8 compatibility: the old top-level output pair is public-only.
    return {
        "document": outputs.get("document") or {"enabled": False},
        "message": outputs.get("message") or {"enabled": False},
    }


def _prepare_report_file(
    state: PortableState,
    paths: RuntimePaths,
    *,
    window: Any,
    events: list[dict[str, Any]],
    content: str,
) -> tuple[str, str, dict[str, Any]]:
    existing = state.report(window.report_key)
    if existing:
        report_path = Path(existing["report_path"])
        if report_path.exists():
            content = report_path.read_text(encoding="utf-8")
        else:
            _write_private_text(report_path, content)
        return content, str(report_path), existing
    report_path = paths.reports / f"{window.report_key}-{window.audience}.md"
    _write_private_text(report_path, content)
    report = state.prepare_report(
        key=window.report_key,
        audience=window.audience,
        output_kind=window.output_kind,
        period=window.period,
        window_start=window.start,
        window_end=window.end,
        title=window.title,
        report_path=str(report_path),
        content_hash=report_content_hash(content),
        event_ids=[event["event_id"] for event in events],
    )
    return content, str(report_path), report


def _prepare_developer_digest(
    state: PortableState,
    paths: RuntimePaths,
    config: dict[str, Any],
    *,
    output_kind: str,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Prepare only the developer stream without reserving public report state.

    Review and master-bridge gates routinely need to deliver diagnostics before a
    public report is allowed.  Resolving or preparing the public stream here would
    freeze that calendar period before the host Agent can approve its facts.
    """

    if config.get("state") != "enabled":
        raise ValueError("tracking is not enabled")
    generated_at = at or datetime.now(timezone.utc)
    developer_window = resolve_report_window(
        state,
        audience="developer",
        output_kind=output_kind,
        timezone_name=config["schedule"]["timezone"],
        at=generated_at,
    )
    materialize_window_events(
        state,
        window_start=developer_window.start,
        window_end=developer_window.end,
    )
    review_path = (
        paths.reports / f"{developer_window.report_key}-agent-review-requests.json"
    )
    review_bundle = write_agent_review_bundle(
        state, review_path, batch_size=_agent_review_batch_size(config)
    )
    existing = state.report(developer_window.report_key)
    events = (
        state.report_events(developer_window.report_key)
        if existing
        else state.events(
            audience="developer",
            window_start=developer_window.start,
            window_end=developer_window.end,
        )
    )
    content = render_developer_report(
        developer_window,
        events,
        review_bundle_path=str(review_path),
    )
    report, report_path, report_ledger = _prepare_report_file(
        state,
        paths,
        window=developer_window,
        events=events,
        content=content,
    )
    delivery_key = f"{developer_window.report_key}:delivery"
    delivery = state.prepare_delivery(
        key=delivery_key,
        output_kind=output_kind,
        period=developer_window.period,
        title=developer_window.title,
        report_path=report_path,
        audience="developer",
        report_key=developer_window.report_key,
        window_start=developer_window.start,
        window_end=developer_window.end,
    )
    return {
        "delivery_key": delivery_key,
        "title": developer_window.title,
        "period": developer_window.period,
        "window": window_dict(developer_window),
        "report": report,
        "report_path": report_path,
        "report_ledger": report_ledger,
        "delivery": delivery,
        "events": events,
        "review_requests": {
            "path": str(review_path),
            "count": len(review_bundle["requests"]),
            "schema_version": review_bundle["schema_version"],
            "snapshot_id": review_bundle["review_snapshot"]["snapshot_id"],
        },
    }


def prepare_digest(
    state: PortableState,
    paths: RuntimePaths,
    config: dict[str, Any],
    *,
    output_kind: str,
    tracker_run_id: str | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    if config.get("state") != "enabled":
        raise ValueError("tracking is not enabled")
    del tracker_run_id  # v0.9 reports a durable time window, never one scan run.
    generated_at = at or datetime.now(timezone.utc)
    timezone_name = config["schedule"]["timezone"]
    public_window = resolve_report_window(
        state,
        audience="public",
        output_kind=output_kind,
        timezone_name=timezone_name,
        at=generated_at,
    )
    developer_window = resolve_report_window(
        state,
        audience="developer",
        output_kind=output_kind,
        timezone_name=timezone_name,
        at=generated_at,
    )
    materialize_window_events(
        state,
        window_start=min(public_window.start, developer_window.start),
        window_end=max(public_window.end, developer_window.end),
    )
    review_path = paths.reports / f"{public_window.report_key}-agent-review-requests.json"
    review_bundle = write_agent_review_bundle(
        state, review_path, batch_size=_agent_review_batch_size(config)
    )

    existing_public = state.report(public_window.report_key)
    public_events = (
        state.report_events(public_window.report_key)
        if existing_public
        else state.unreported_public_events(
            through=public_window.end, output_kind=output_kind
        )
    )
    public_content = render_public_report(public_window, public_events)
    report, report_path, report_ledger = _prepare_report_file(
        state,
        paths,
        window=public_window,
        events=public_events,
        content=public_content,
    )
    existing_developer = state.report(developer_window.report_key)
    developer_events = (
        state.report_events(developer_window.report_key)
        if existing_developer
        else state.events(
            audience="developer",
            window_start=developer_window.start,
            window_end=developer_window.end,
        )
    )
    developer_content = render_developer_report(
        developer_window,
        developer_events,
        review_bundle_path=str(review_path),
    )
    developer_report, developer_report_path, developer_ledger = _prepare_report_file(
        state,
        paths,
        window=developer_window,
        events=developer_events,
        content=developer_content,
    )

    delivery_key = f"{public_window.report_key}:delivery"
    delivery = state.prepare_delivery(
        key=delivery_key,
        output_kind=output_kind,
        period=public_window.period,
        title=public_window.title,
        report_path=report_path,
        audience="public",
        report_key=public_window.report_key,
        window_start=public_window.start,
        window_end=public_window.end,
    )
    developer_delivery_key = f"{developer_window.report_key}:delivery"
    developer_delivery = state.prepare_delivery(
        key=developer_delivery_key,
        output_kind=output_kind,
        period=developer_window.period,
        title=developer_window.title,
        report_path=developer_report_path,
        audience="developer",
        report_key=developer_window.report_key,
        window_start=developer_window.start,
        window_end=developer_window.end,
    )
    return {
        "delivery_key": delivery_key,
        "title": public_window.title,
        "period": public_window.period,
        "window": window_dict(public_window),
        "report": report,
        "report_path": report_path,
        "report_ledger": report_ledger,
        "delivery": delivery,
        "events": public_events,
        "stats": public_event_stats(public_events),
        "review_requests": {
            "path": str(review_path),
            "count": len(review_bundle["requests"]),
            "schema_version": review_bundle["schema_version"],
            "snapshot_id": review_bundle["review_snapshot"]["snapshot_id"],
        },
        "developer": {
            "delivery_key": developer_delivery_key,
            "title": developer_window.title,
            "period": developer_window.period,
            "window": window_dict(developer_window),
            "report": developer_report,
            "report_path": developer_report_path,
            "report_ledger": developer_ledger,
            "delivery": developer_delivery,
            "events": developer_events,
        },
    }


def export_agent_review_requests(
    state: PortableState,
    output_path: str | Path,
    *,
    since: datetime | None = None,
    through: datetime | None = None,
    batch_size: int = 100,
) -> dict[str, Any]:
    end = through or datetime.now(timezone.utc)
    start = since or end - timedelta(days=8)
    materialized = materialize_window_events(
        state,
        window_start=start.astimezone(timezone.utc).isoformat(),
        window_end=end.astimezone(timezone.utc).isoformat(),
    )
    bundle = write_agent_review_bundle(state, output_path, batch_size=batch_size)
    return {
        **bundle,
        "materialized": materialized,
        "window_start": start.astimezone(timezone.utc).isoformat(),
        "window_end": end.astimezone(timezone.utc).isoformat(),
    }


def apply_agent_review_results(
    state: PortableState, payload: dict[str, Any]
) -> dict[str, Any]:
    return apply_agent_review_decisions(state, payload)


def _developer_message(prepared: dict[str, Any], doc_url: str | None) -> str:
    events = prepared["events"]
    counts = _counts(event["event_type"] for event in events)
    lines = [
        "**人员跟踪开发者运行报告**",
        "",
        f"本窗口记录 {len(events)} 个运维事件。",
    ]
    for event_type, count in sorted(counts.items()):
        lines.append(f"- {event_type}: {count}")
    if doc_url:
        lines.extend(["", f"[查看开发者报告]({markdown_http_url(doc_url)})"])
    return "\n".join(lines)[:2950]


def _deliver_prepared(
    state: PortableState,
    config: dict[str, Any],
    *,
    prepared: dict[str, Any],
    audience: str,
    apply: bool,
    lark: LarkCli | None,
) -> dict[str, Any]:
    key = prepared["delivery_key"]
    existing = state.delivery(key) or {}
    if existing.get("status") == "completed":
        return {**prepared, "idempotent_replay": True, "actions": []}
    actions: list[dict[str, Any]] = []
    outputs = _audience_outputs(config, audience)
    document = outputs.get("document") or {"enabled": False}
    message = outputs.get("message") or {"enabled": False}
    events = prepared.get("events") or []

    # A public empty period advances its durable cursor but never creates an empty
    # document/message.  Developer local reports remain independently deliverable.
    if audience == "public" and not events:
        if apply:
            state.update_delivery(key, status="completed")
        return {
            **prepared,
            "idempotent_replay": False,
            "apply": apply,
            "empty_public_report_suppressed": True,
            "actions": [],
            "delivery": state.delivery(key),
        }

    doc_url = existing.get("doc_url")
    if document.get("enabled") and not doc_url:
        if lark is None:
            actions.append(
                {
                    "adapter": "agent_tool_bridge",
                    "operation": "create_document",
                    "audience": audience,
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
                doc_url = markdown_http_url(doc_url)
                state.update_delivery(
                    key,
                    doc_url=str(doc_url),
                    doc_token=str(doc_token or ""),
                    status="document_created",
                )
    message_text = (
        render_public_message(events, str(doc_url) if doc_url else None)
        if audience == "public"
        else _developer_message(prepared, str(doc_url) if doc_url else None)
    )
    if message.get("enabled"):
        target_kind = str(message.get("target_kind") or "current_chat")
        target_id = str(message.get("target_id") or "")
        if lark is None or target_kind == "current_chat":
            actions.append(
                {
                    "adapter": "agent_tool_bridge",
                    "operation": "send_current_or_configured_message",
                    "audience": audience,
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
    return _deliver_prepared(
        state,
        config,
        prepared=prepared,
        audience="public",
        apply=apply,
        lark=lark,
    )


def deliver_developer_digest(
    state: PortableState,
    paths: RuntimePaths,
    config: dict[str, Any],
    *,
    output_kind: str,
    apply: bool,
    lark: LarkCli | None,
    at: datetime | None = None,
) -> dict[str, Any]:
    prepared = _prepare_developer_digest(
        state,
        paths,
        config,
        output_kind=output_kind,
        at=at,
    )
    return _deliver_prepared(
        state,
        config,
        prepared=prepared,
        audience="developer",
        apply=apply,
        lark=lark,
    )
