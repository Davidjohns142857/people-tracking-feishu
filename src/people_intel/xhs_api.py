from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


READ_ONLY_COMMANDS = frozenset({"status", "search", "read", "user", "user-posts"})
FALLBACK_ERROR_CODES = frozenset({"signature_error"})
RISK_ERROR_CODES = frozenset({"verification_required", "ip_blocked"})


@dataclass(frozen=True)
class XhsApiCircuitState:
    status: str = "closed"
    reason: str | None = None
    opened_at_epoch: float | None = None
    retry_at_epoch: float | None = None
    fallback_allowed: bool = False

    @property
    def active(self) -> bool:
        return (
            self.status == "open"
            and self.retry_at_epoch is not None
            and time.time() < self.retry_at_epoch
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": "open" if self.active else "closed",
            "reason": self.reason if self.active else None,
            "retry_at_epoch": self.retry_at_epoch if self.active else None,
            "fallback_allowed": self.fallback_allowed if self.active else False,
        }


class XhsApiRouteCircuit:
    """Persistent route-level circuit for the reverse-engineered API transport.

    The account-level connector policy remains responsible for quotas and
    minimum intervals. This smaller circuit only decides whether the preferred
    API transport may be attempted or the already validated browser transport
    should be selected for the next job.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> XhsApiCircuitState:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return XhsApiCircuitState(
                status=str(payload.get("status") or "closed"),
                reason=str(payload["reason"]) if payload.get("reason") else None,
                opened_at_epoch=float(payload["opened_at_epoch"]) if payload.get("opened_at_epoch") else None,
                retry_at_epoch=float(payload["retry_at_epoch"]) if payload.get("retry_at_epoch") else None,
                fallback_allowed=bool(payload.get("fallback_allowed", False)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return XhsApiCircuitState()

    def open(self, *, reason: str, cooldown_seconds: int, fallback_allowed: bool) -> XhsApiCircuitState:
        now = time.time()
        state = XhsApiCircuitState(
            status="open",
            reason=reason,
            opened_at_epoch=now,
            retry_at_epoch=now + max(60, cooldown_seconds),
            fallback_allowed=fallback_allowed,
        )
        self._write(state)
        return state

    def close(self) -> None:
        self._write(XhsApiCircuitState())

    def _write(self, state: XhsApiCircuitState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "status": state.status,
                    "reason": state.reason,
                    "opened_at_epoch": state.opened_at_epoch,
                    "retry_at_epoch": state.retry_at_epoch,
                    "fallback_allowed": state.fallback_allowed,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class XhsApiCliTransport:
    """Strict read-only wrapper around jackwener/xiaohongshu-cli."""

    def __init__(
        self,
        root: str | Path,
        home: str | Path,
        *,
        timeout_seconds: int = 120,
        max_bytes: int = 4_000_000,
    ):
        self.root = Path(root).expanduser().resolve()
        self.home = Path(home).expanduser().resolve()
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    @property
    def executable(self) -> Path:
        return self.root / ".venv" / "bin" / "xhs"

    @property
    def credential_path(self) -> Path:
        return self.home / ".xiaohongshu-cli" / "cookies.json"

    @property
    def available(self) -> bool:
        return self.executable.is_file()

    @property
    def credentials_configured(self) -> bool:
        return self.credential_path.is_file()

    def run(self, command: str, *args: str) -> dict[str, Any]:
        if command not in READ_ONLY_COMMANDS:
            raise ValueError(f"XHS API command is not in the read-only allowlist: {command}")
        if not self.available:
            raise ValueError("xiaohongshu-cli executable is unavailable")
        self.home.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            [str(self.executable), command, *args, "--json"],
            shell=False,
            check=False,
            capture_output=True,
            timeout=self.timeout_seconds,
            env={
                **os.environ,
                "HOME": str(self.home),
                "OUTPUT": "json",
                "PYTHONUNBUFFERED": "1",
            },
        )
        if len(process.stdout) > self.max_bytes:
            raise ValueError("xiaohongshu-cli response exceeded output limit")
        try:
            payload = json.loads(process.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            detail = (process.stderr or process.stdout).decode("utf-8", errors="replace")
            raise ValueError(f"xiaohongshu-cli returned invalid JSON: {detail[:600]}") from exc
        if not isinstance(payload, dict):
            raise ValueError("xiaohongshu-cli returned a non-object JSON payload")
        if process.returncode != 0 and payload.get("ok") is not False:
            detail = (process.stderr or process.stdout).decode("utf-8", errors="replace")
            raise ValueError(f"xiaohongshu-cli exited {process.returncode}: {detail[:600]}")
        return payload


def envelope_error(payload: dict[str, Any]) -> tuple[str, str] | None:
    if payload.get("ok") is not False:
        return None
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = str(error.get("code") or "api_error")
    message = str(error.get("message") or "xiaohongshu-cli request failed")
    return code, message


__all__ = [
    "FALLBACK_ERROR_CODES",
    "READ_ONLY_COMMANDS",
    "RISK_ERROR_CODES",
    "XhsApiCliTransport",
    "XhsApiCircuitState",
    "XhsApiRouteCircuit",
    "envelope_error",
]
