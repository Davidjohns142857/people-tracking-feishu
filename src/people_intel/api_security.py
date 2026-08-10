from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApiSecurityConfig:
    """Runtime-only API security settings.

    The token value is deliberately excluded from representations and public
    manifests. A token file is preferred so service definitions and process
    listings never contain the credential.
    """

    token: str = ""
    token_source: str = "disabled"
    max_request_bytes: int = 10_485_760

    @classmethod
    def from_environment(cls) -> "ApiSecurityConfig":
        token_file = os.environ.get("PEOPLE_INTEL_API_TOKEN_FILE", "").strip()
        token = ""
        source = "disabled"
        if token_file:
            path = Path(token_file).expanduser()
            if not path.is_file():
                raise RuntimeError("PEOPLE_INTEL_API_TOKEN_FILE does not exist")
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise RuntimeError("PEOPLE_INTEL_API_TOKEN_FILE must not be group/world accessible")
            token = path.read_text(encoding="utf-8").strip()
            source = "file"
        elif os.environ.get("PEOPLE_INTEL_API_TOKEN"):
            token = os.environ["PEOPLE_INTEL_API_TOKEN"].strip()
            source = "environment"
        if token and len(token) < 32:
            raise RuntimeError("PEOPLE_INTEL_API_TOKEN must contain at least 32 characters")
        max_bytes = max(
            1024,
            min(
                100 * 1024 * 1024,
                int(os.environ.get("PEOPLE_INTEL_API_MAX_REQUEST_BYTES", "10485760")),
            ),
        )
        return cls(token=token, token_source=source, max_request_bytes=max_bytes)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def accepts(self, authorization: str | None) -> bool:
        if not self.enabled:
            return True
        if not authorization or not authorization.startswith("Bearer "):
            return False
        supplied = authorization.removeprefix("Bearer ").strip()
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def public_status(self) -> dict[str, object]:
        return {
            "authentication_required": self.enabled,
            "credential_source": self.token_source,
            "max_request_bytes": self.max_request_bytes,
            "token_exposed": False,
        }


__all__ = ["ApiSecurityConfig"]
