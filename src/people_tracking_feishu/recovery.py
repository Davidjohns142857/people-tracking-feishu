"""Bounded midweek recovery, separate from source health and fact baselines."""

from __future__ import annotations

import platform
import subprocess
import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from people_intel.light_cli import _uses_loopback_proxy, _validate_public_url

from .state import PortableState

MAX_RECOVERY_ATTEMPTS = 5
_PERMANENT = (
    "certificate",
    "ssl:",
    "tlsv",
    "authwall",
    "authentication wall",
    "auth wall",
    "captcha",
    "login required",
    "hostname mismatch",
    "no shared cipher",
    "no suitable key share",
    "unsupported protocol",
    "wrong_version_number",
    "wrong version number",
    "unsafe legacy renegotiation",
)
_DNS = (
    "public hostname did not resolve",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "getaddrinfo failed",
    "no address associated",
)


def recoverable_kind(observation: Any) -> str | None:
    status = int(observation.status_code or 0)
    detail = str(observation.error or "").casefold()
    body = observation.body
    if isinstance(body, bytes):
        body = body[:8192].decode("utf-8", errors="ignore")
    if isinstance(body, str):
        detail += " " + body[:8192].casefold()
    if status not in {0, 408, 425, 500, 502, 503, 504} or any(
        x in detail for x in _PERMANENT
    ):
        return None
    if "resolved to a non-public address" in detail:
        return "proxy_dns_configuration"
    if any(x in detail for x in _DNS):
        return "dns_resolution"
    if status in {408, 425, 500, 502, 503, 504} or any(
        x in detail
        for x in (
            "timeout",
            "timed out",
            "reset",
            "eof",
            "incompleteread",
            "connection refused",
            "network is unreachable",
        )
    ):
        return "temporary_transport"
    return None


def flush_dns_cache() -> str:
    """Refresh cached resolver state using a fixed, unprivileged OS command.

    Never changes nameservers, proxy settings, /etc/hosts, or TLS policy. A zero
    return code is only a repair attempt; only a healthy refetch proves recovery.
    """
    command = {
        "Darwin": ["/usr/bin/dscacheutil", "-flushcache"],
        "Linux": ["/usr/bin/resolvectl", "flush-caches"],
    }.get(platform.system())
    if not command or not Path(command[0]).is_file():
        return "unsupported"
    try:
        result = subprocess.run(command, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "failed"
    return "attempted" if result.returncode == 0 else "permission_or_command_failed"


def diagnose_dns(source: dict[str, Any]) -> dict[str, Any]:
    """Classify resolver/proxy mismatch without recording addresses or credentials."""
    try:
        url = _validate_public_url(str(source["url"]), resolve=False)
        parsed = urlsplit(url)
        proxied = _uses_loopback_proxy(parsed.scheme, parsed.hostname or "")
        values = {
            ipaddress.ip_address(r[4][0])
            for r in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError):
        return {"resolution": "unresolved_or_rejected"}
    fake = (
        bool(values)
        and all(
            a in ipaddress.ip_network("198.18.0.0/15") for a in values if a.version == 4
        )
        and all(a.version == 4 for a in values)
    )
    if fake:
        status = (
            "fake_ip_with_proxy"
            if proxied
            else "fake_ip_missing_effective_loopback_proxy"
        )
    else:
        status = (
            "public"
            if values and all(a.is_global for a in values)
            else "non_public_blocked"
        )
    return {"resolution": status, "effective_loopback_proxy": proxied}


def recovery_due(state: PortableState, *, now: datetime) -> bool:
    queue = state.get_meta("network_recovery_queue", {})
    enabled = {
        r[0]
        for r in state.db.execute(
            "SELECT source_id FROM sources WHERE tracking_enabled=1"
        )
    }
    return any(
        sid in enabled
        and entry["status"] == "pending"
        and entry["next_retry_at"] <= now.isoformat()
        for sid, entry in queue.items()
    )


class RecoveryManager:
    def __init__(self, state: PortableState, run_id: str):
        self.state, self.run_id = state, run_id
        self.queue = state.get_meta("network_recovery_queue", {})
        self.metrics: dict[str, Any] = {
            "repair_attempts": [],
            "refetches": 0,
            "recovered": 0,
            "deferred": 0,
            "escalated": 0,
            "circuit_deferred": 0,
            "attention_skipped": 0,
            "dns_diagnostics": [],
        }
        self.dns_hosts: set[str] = set()
        self.circuit_open = False
        self.attempted_sources: set[str] = set()

    def recover(self, source: dict[str, Any], observation: Any, fetch: Callable) -> Any:
        kind = recoverable_kind(observation)
        if not kind:
            return observation
        sid = source["source_id"]
        prior = self.queue.get(sid, {})
        if prior.get("status") == "needs_attention":
            return observation
        if kind in {"dns_resolution", "proxy_dns_configuration"}:
            self.metrics["dns_diagnostics"].append(
                {"source_id": sid, **diagnose_dns(source)}
            )
            now = datetime.now(timezone.utc)
            last = self.state.get_meta("network_dns_cache_refresh_at")
            if not last or datetime.fromisoformat(last) <= now - timedelta(hours=1):
                # Persist before executing so a restart cannot cause a flush loop.
                self.state.set_meta("network_dns_cache_refresh_at", now.isoformat())
                self.metrics["repair_attempts"].append(
                    {"action": "refresh_dns_cache", "result": flush_dns_cache()}
                )
        # Scholar attempts belong exclusively to its persistent host-budget/canary
        # path; Homepage already has one transient transport retry.
        if source.get("kind") == "scholar" or (
            source.get("kind") == "homepage" and kind == "temporary_transport"
        ):
            return observation
        self.metrics["refetches"] += 1
        self.attempted_sources.add(sid)
        return fetch(source)

    def finish(
        self, source: dict[str, Any], observation: Any, *, healthy: bool = False
    ) -> bool:
        """Return true if infrastructure failure must NOT become a source observation."""
        sid = source["source_id"]
        kind = recoverable_kind(observation)
        if healthy:
            if sid in self.queue or sid in self.attempted_sources:
                self.metrics["recovered"] += 1
                self.queue.pop(sid, None)
                self._save()
            return False
        if not kind:
            # A definitive non-retryable response closes transport recovery. Its
            # own source incident remains visible through the normal health path.
            if sid in self.queue:
                self.queue.pop(sid)
                self._save()
            return False
        prior = self.queue.get(sid, {})
        attempts = min(MAX_RECOVERY_ATTEMPTS, int(prior.get("attempts", 0)) + 1)
        now = datetime.now(timezone.utc)
        retry = now + timedelta(minutes=min(360, 15 * 2 ** min(attempts - 1, 5)))
        entry = {
            "kind": kind,
            "attempts": attempts,
            "status": (
                "pending" if attempts < MAX_RECOVERY_ATTEMPTS else "needs_attention"
            ),
            "next_retry_at": retry.isoformat(),
            "last_run_id": self.run_id,
        }
        self.queue[sid] = entry
        self.state.db.execute(
            "UPDATE sources SET next_check_at=? WHERE source_id=? AND tracking_enabled=1",
            (retry.isoformat(), sid),
        )
        self.state.db.commit()
        self.metrics["deferred"] += 1
        if entry["status"] == "needs_attention":
            self.metrics["escalated"] += 1
        self._save()
        if kind in {"dns_resolution", "proxy_dns_configuration"}:
            self.dns_hosts.add(urlsplit(str(source["url"])).hostname or "")
            self.circuit_open = len(self.dns_hosts) >= 5
            return True
        return False

    def defer_after_circuit(self, source: dict[str, Any]) -> None:
        # No fetch, observation, or failure count. Resume through the same queue.
        sid = source["source_id"]
        prior = self.queue.get(sid, {})
        retry = datetime.now(timezone.utc) + timedelta(minutes=15)
        self.queue[sid] = {
            "kind": "infrastructure_circuit",
            "attempts": prior.get("attempts", 0),
            "status": "pending",
            "next_retry_at": retry.isoformat(),
            "last_run_id": self.run_id,
        }
        self.state.db.execute(
            "UPDATE sources SET next_check_at=? WHERE source_id=? AND tracking_enabled=1",
            (retry.isoformat(), sid),
        )
        self.state.db.commit()
        self.metrics["circuit_deferred"] += 1
        self._save()

    def _save(self) -> None:
        self.state.set_meta("network_recovery_queue", self.queue)
