from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


ALLOWED_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}
ALLOWED_SOURCE_KINDS = {"homepage", "scholar", "linkedin"}
ALLOWED_RETRIEVAL_MODES = {"direct", "browser_public"}
ALLOWED_DECISIONS = {"meaningful_change", "noise", "uncertain"}
ALLOWED_CHANGE_TYPES = {
    "identity",
    "affiliation",
    "education",
    "position",
    "publication",
    "project",
    "award",
    "research_topic",
    "profile_edit",
    "other",
}
DEFAULT_LOCAL_CONFIG = Path("~/.config/people-intel/deepseek.json").expanduser()
SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|cookie|set-cookie|password|secret|token|raw_html|page_body)",
    re.I,
)
SENSITIVE_VALUE = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer|bearer\s+|sk-)[A-Za-z0-9._~+/=-]{12,}"
)
INLINE_SECRET_CONFIG_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie|authorization)(?:$|[_-])",
    re.I,
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _integer_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class DeepSeekSettings:
    enabled: bool = False
    api_key_file: Path | None = None
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    timeout_seconds: int = 40
    max_input_chars: int = 12_000
    max_output_tokens: int = 800
    max_calls_per_source_per_day: int = 2
    max_calls_per_person_per_week: int = 10
    max_calls_per_day: int = 500
    max_total_tokens_per_day: int = 500_000
    audit_database: Path = Path(".people_intel/deepseek-audit.sqlite3")
    configuration_source: str = "direct"

    @classmethod
    def from_env(cls) -> "DeepSeekSettings":
        key_file = os.environ.get("PEOPLE_INTEL_DEEPSEEK_API_KEY_FILE")
        return cls(
            enabled=_truthy(os.environ.get("PEOPLE_INTEL_DEEPSEEK_ENABLED")),
            api_key_file=Path(key_file).expanduser() if key_file else None,
            base_url=os.environ.get(
                "PEOPLE_INTEL_DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            model=os.environ.get(
                "PEOPLE_INTEL_DEEPSEEK_MODEL", "deepseek-v4-flash"
            ),
            timeout_seconds=_integer_env(
                "PEOPLE_INTEL_DEEPSEEK_TIMEOUT_SECONDS", 40, 5, 120
            ),
            max_input_chars=_integer_env(
                "PEOPLE_INTEL_DEEPSEEK_MAX_INPUT_CHARS", 12_000, 2_000, 40_000
            ),
            max_output_tokens=_integer_env(
                "PEOPLE_INTEL_DEEPSEEK_MAX_OUTPUT_TOKENS", 800, 200, 2_000
            ),
            max_calls_per_source_per_day=_integer_env(
                "PEOPLE_INTEL_DEEPSEEK_MAX_CALLS_PER_SOURCE_PER_DAY", 2, 1, 10
            ),
            max_calls_per_person_per_week=_integer_env(
                "PEOPLE_INTEL_DEEPSEEK_MAX_CALLS_PER_PERSON_PER_WEEK", 10, 1, 20
            ),
            max_calls_per_day=_integer_env(
                "PEOPLE_INTEL_DEEPSEEK_MAX_CALLS_PER_DAY", 500, 1, 10_000
            ),
            max_total_tokens_per_day=_integer_env(
                "PEOPLE_INTEL_DEEPSEEK_MAX_TOTAL_TOKENS_PER_DAY",
                500_000,
                1_000,
                10_000_000,
            ),
            audit_database=Path(
                os.environ.get(
                    "PEOPLE_INTEL_DEEPSEEK_AUDIT_DB",
                    ".people_intel/deepseek-audit.sqlite3",
                )
            ).expanduser(),
            configuration_source="environment",
        )

    @classmethod
    def from_config_file(cls, path: str | Path) -> "DeepSeekSettings":
        config_path = Path(path).expanduser()
        try:
            mode = config_path.stat().st_mode
        except OSError as exc:
            raise ValueError("DeepSeek config file is missing or unreadable") from exc
        if config_path.is_symlink() or not stat.S_ISREG(mode):
            raise ValueError("DeepSeek config must be a regular non-symlink file")
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("DeepSeek config file must be mode 0600")
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("DeepSeek config is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("DeepSeek config must be a JSON object")
        forbidden = {
            key
            for key in payload
            if INLINE_SECRET_CONFIG_KEY.search(str(key)) and key != "api_key_file"
        }
        if forbidden:
            raise ValueError("DeepSeek config must not contain inline secrets")
        allowed = {
            "enabled",
            "api_key_file",
            "base_url",
            "model",
            "timeout_seconds",
            "max_input_chars",
            "max_output_tokens",
            "max_calls_per_source_per_day",
            "max_calls_per_person_per_week",
            "max_calls_per_day",
            "max_total_tokens_per_day",
            "audit_database",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(
                "DeepSeek config contains unsupported keys: "
                + ", ".join(sorted(unknown))
            )

        def configured_path(name: str, default: Path | None = None) -> Path | None:
            raw = payload.get(name)
            if raw in (None, ""):
                return default
            candidate = Path(str(raw)).expanduser()
            if not candidate.is_absolute():
                candidate = config_path.parent / candidate
            return candidate

        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("DeepSeek config enabled must be a JSON boolean")
        try:
            values: dict[str, Any] = {
                "enabled": enabled,
                "api_key_file": configured_path("api_key_file"),
                "base_url": str(
                    payload.get("base_url") or "https://api.deepseek.com"
                ).rstrip("/"),
                "model": str(payload.get("model") or "deepseek-v4-flash"),
                "timeout_seconds": int(payload.get("timeout_seconds", 40)),
                "max_input_chars": int(payload.get("max_input_chars", 12_000)),
                "max_output_tokens": int(payload.get("max_output_tokens", 800)),
                "max_calls_per_source_per_day": int(
                    payload.get("max_calls_per_source_per_day", 2)
                ),
                "max_calls_per_person_per_week": int(
                    payload.get("max_calls_per_person_per_week", 10)
                ),
                "max_calls_per_day": int(payload.get("max_calls_per_day", 500)),
                "max_total_tokens_per_day": int(
                    payload.get("max_total_tokens_per_day", 500_000)
                ),
                "audit_database": configured_path(
                    "audit_database",
                    Path(
                        "~/.local/state/people-intel/deepseek-audit.sqlite3"
                    ).expanduser(),
                ),
                "configuration_source": "config_file",
            }
        except (TypeError, ValueError) as exc:
            raise ValueError("DeepSeek config contains an invalid numeric value") from exc
        settings = cls(**values)
        numeric_errors = []
        for name, minimum, maximum in (
            ("timeout_seconds", 5, 120),
            ("max_input_chars", 2_000, 40_000),
            ("max_output_tokens", 200, 2_000),
            ("max_calls_per_source_per_day", 1, 10),
            ("max_calls_per_person_per_week", 1, 20),
            ("max_calls_per_day", 1, 10_000),
            ("max_total_tokens_per_day", 1_000, 10_000_000),
        ):
            value = int(getattr(settings, name))
            if not minimum <= value <= maximum:
                numeric_errors.append(f"{name} must be between {minimum} and {maximum}")
        if numeric_errors:
            raise ValueError("; ".join(numeric_errors))
        return settings

    @classmethod
    def from_runtime(
        cls,
        config_file: str | Path | None = None,
    ) -> "DeepSeekSettings":
        environment = cls.from_env()
        if environment.enabled:
            return environment
        path = Path(config_file).expanduser() if config_file else DEFAULT_LOCAL_CONFIG
        if path.exists():
            return cls.from_config_file(path)
        return environment

    def validate_public(self) -> list[str]:
        errors: list[str] = []
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.deepseek.com":
            errors.append(
                "base URL must be exactly the official HTTPS api.deepseek.com host"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            errors.append("base URL must not contain credentials, query, or fragment")
        if self.model not in ALLOWED_MODELS:
            errors.append("model is not in the package allowlist")
        if self.enabled and self.api_key_file is None:
            errors.append("enabled runtime requires a separate API key file")
        if self.enabled and os.environ.get("DEEPSEEK_API_KEY"):
            errors.append(
                "raw DEEPSEEK_API_KEY environment variables are rejected; use the key file"
            )
        if self.enabled and os.environ.get("PEOPLE_INTEL_DEEPSEEK_API_KEY"):
            errors.append(
                "raw PEOPLE_INTEL_DEEPSEEK_API_KEY is rejected; use the key file"
            )
        if self.enabled and self.api_key_file is not None:
            try:
                mode = self.api_key_file.stat().st_mode
                if self.api_key_file.is_symlink():
                    errors.append("API key file must not be a symlink")
                if not stat.S_ISREG(mode):
                    errors.append("API key path is not a regular file")
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    errors.append("API key file must be mode 0600")
                size = self.api_key_file.stat().st_size
                if not 20 <= size <= 512:
                    errors.append("API key file has an invalid size")
            except OSError:
                errors.append("API key file is missing or unreadable")
        return errors

    def public_status(self) -> dict[str, Any]:
        errors = self.validate_public()
        return {
            "enabled": self.enabled,
            "configured": self.enabled and not errors,
            "model": self.model,
            "configuration_source": self.configuration_source,
            "base_host": urlparse(self.base_url).hostname,
            "key_transport": "mode-0600-file-only",
            "key_value_returned": False,
            "raw_environment_key_accepted": False,
            "eligible_source_kinds": sorted(ALLOWED_SOURCE_KINDS),
            "eligible_retrieval_modes": sorted(ALLOWED_RETRIEVAL_MODES),
            "use_cases": ["ambiguous_review", "confirmed_summary"],
            "model_authority": {
                "ambiguous_review": "advisory_only",
                "confirmed_summary": "wording_only",
            },
            "max_input_chars": self.max_input_chars,
            "max_output_tokens": self.max_output_tokens,
            "budgets": {
                "calls_per_source_per_day": self.max_calls_per_source_per_day,
                "calls_per_person_per_week": self.max_calls_per_person_per_week,
                "calls_per_day": self.max_calls_per_day,
                "total_tokens_per_day": self.max_total_tokens_per_day,
            },
            "errors": errors,
        }


class DeepSeekPolicyError(RuntimeError):
    pass


class DeepSeekResponseError(RuntimeError):
    pass


Transport = Callable[[dict[str, Any], dict[str, str], int], dict[str, Any]]


class DeepSeekDiffReviewer:
    """Advisory-only fallback for unresolved semantic webpage diffs."""

    def __init__(
        self,
        settings: DeepSeekSettings | None = None,
        *,
        transport: Transport | None = None,
    ):
        self.settings = settings or DeepSeekSettings.from_env()
        errors = self.settings.validate_public()
        if errors:
            raise DeepSeekPolicyError("; ".join(errors))
        if not self.settings.enabled:
            raise DeepSeekPolicyError("DeepSeek fallback is disabled")
        self.transport = transport or self._http_transport
        self._prepare_audit_database()

    @classmethod
    def from_env(
        cls, *, transport: Transport | None = None
    ) -> "DeepSeekDiffReviewer | None":
        settings = DeepSeekSettings.from_env()
        if not settings.enabled:
            return None
        return cls(settings, transport=transport)

    @classmethod
    def from_runtime(
        cls,
        config_file: str | Path | None = None,
        *,
        required: bool = False,
        transport: Transport | None = None,
    ) -> "DeepSeekDiffReviewer | None":
        settings = DeepSeekSettings.from_runtime(config_file)
        if not settings.enabled:
            if required:
                raise DeepSeekPolicyError(
                    "DeepSeek is not configured; create a mode-0600 local config/key file"
                )
            return None
        return cls(settings, transport=transport)

    def __call__(self, decision: Any) -> Any:
        return self.review(decision, context={})

    def review(self, decision: Any, *, context: dict[str, Any]) -> Any:
        eligible, reason = self.eligibility(decision, context)
        if not eligible:
            raise DeepSeekPolicyError(f"ineligible fallback request: {reason}")

        evidence = self._evidence(decision)
        if not evidence:
            raise DeepSeekPolicyError("no compact delta evidence")
        parsed = self._invoke(
            evidence=evidence,
            context=context,
            reason=reason,
            purpose="ambiguous_review",
        )

        decision.reviewer = f"deepseek:{self.settings.model}:advisory"
        decision.summary = str(parsed["summary_zh"])
        if parsed["decision"] == "meaningful_change":
            decision.status = "changed"
            decision.summary = (
                f"DeepSeek 仅建议该差异可能有意义：{decision.summary}；"
                "尚未确认，必须通过既有一致观测门"
            )
        elif parsed["decision"] == "noise":
            decision.status = "noise"
            decision.summary = f"DeepSeek 建议该差异为页面噪声：{decision.summary}"
        else:
            decision.status = "ambiguous"
            decision.summary = f"DeepSeek 无法确定：{decision.summary}"
        decision.score = min(
            float(decision.score),
            max(0.0, min(0.74, float(parsed["confidence"]))),
        )
        return decision

    def summarize_confirmed(
        self,
        decision: Any,
        *,
        context: dict[str, Any],
    ) -> Any:
        """Summarize an already-confirmed deterministic delta without re-deciding it."""
        eligible, reason = self.summary_eligibility(decision, context)
        if not eligible:
            raise DeepSeekPolicyError(f"ineligible summary request: {reason}")
        evidence = self._evidence(decision)
        if not evidence:
            raise DeepSeekPolicyError("no compact delta evidence")
        parsed = self._invoke(
            evidence=evidence,
            context=context,
            reason=reason,
            purpose="confirmed_summary",
        )
        if parsed["decision"] != "meaningful_change" or not parsed["evidence_ids"]:
            raise DeepSeekResponseError(
                "confirmed-summary response did not stay grounded in change evidence"
            )
        decision.reviewer = f"deepseek:{self.settings.model}:confirmed-summary"
        decision.summary = f"已确认变化摘要：{parsed['summary_zh']}"
        # Deterministic confirmation remains authoritative. The model is not
        # allowed to change status, score, deltas or confirmation counts.
        return decision

    @staticmethod
    def eligibility(decision: Any, context: dict[str, Any]) -> tuple[bool, str]:
        if getattr(decision, "status", None) != "ambiguous":
            return False, "deterministic stage already reached a decision"
        if context.get("health_status", "healthy") != "healthy":
            return False, "source health gate did not pass"
        if not context.get("identity_gate_passed", False):
            return False, "identity/completeness gate did not pass"
        if float(context.get("quality_score") or 0.0) < 0.70:
            return False, "quality score is below 0.70"
        source_kind = str(context.get("source_kind") or "")
        if source_kind not in ALLOWED_SOURCE_KINDS:
            return False, "source kind is deterministic-only"
        retrieval_mode = str(context.get("retrieval_mode") or "")
        if retrieval_mode not in ALLOWED_RETRIEVAL_MODES:
            return False, "partial/search-index retrieval is not eligible"
        if not context.get("has_confirmed_baseline", False):
            return False, "no confirmed baseline exists"
        if not (
            getattr(decision, "additions", None)
            or getattr(decision, "removals", None)
            or getattr(decision, "modifications", None)
        ):
            return False, "there is no compact delta"
        return True, "ambiguous_healthy_identity_verified_compact_delta"

    @staticmethod
    def summary_eligibility(
        decision: Any,
        context: dict[str, Any],
    ) -> tuple[bool, str]:
        if getattr(decision, "status", None) != "changed":
            return False, "change is not deterministically confirmed"
        if not context.get("change_confirmed", False):
            return False, "confirmation gate has not completed"
        if context.get("health_status", "healthy") != "healthy":
            return False, "source health gate did not pass"
        if not context.get("identity_gate_passed", False):
            return False, "identity/completeness gate did not pass"
        if float(context.get("quality_score") or 0.0) < 0.70:
            return False, "quality score is below 0.70"
        if str(context.get("source_kind") or "") not in ALLOWED_SOURCE_KINDS:
            return False, "source kind is deterministic-only"
        if str(context.get("retrieval_mode") or "") not in ALLOWED_RETRIEVAL_MODES:
            return False, "partial/search-index retrieval is not eligible"
        if not (
            getattr(decision, "additions", None)
            or getattr(decision, "removals", None)
            or getattr(decision, "modifications", None)
        ):
            return False, "there is no compact delta"
        return True, "confirmed_healthy_identity_verified_compact_delta_summary"

    def usage_snapshot(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        day = (now - timedelta(days=1)).isoformat()
        week = (now - timedelta(days=7)).isoformat()
        with sqlite3.connect(self.settings.audit_database) as connection:
            day_row = connection.execute(
                """SELECT COUNT(*),COALESCE(SUM(prompt_tokens),0),
                          COALESCE(SUM(completion_tokens),0),
                          COALESCE(SUM(total_tokens),0),
                          COALESCE(SUM(prompt_cache_hit_tokens),0),
                          COALESCE(SUM(prompt_cache_miss_tokens),0),
                          COALESCE(SUM(cache_usage_reported),0)
                   FROM deepseek_audit
                   WHERE created_at>=?
                     AND status IN ('reserved','completed','failed')""",
                (day,),
            ).fetchone()
            week_row = connection.execute(
                """SELECT COUNT(*) FROM deepseek_audit
                   WHERE created_at>=? AND status='completed'""",
                (week,),
            ).fetchone()
            purpose_rows = connection.execute(
                """SELECT purpose,COUNT(*) FROM deepseek_audit
                   WHERE created_at>=?
                     AND status IN ('reserved','completed','failed')
                   GROUP BY purpose""",
                (day,),
            ).fetchall()
        purpose_counts = {str(row[0]): int(row[1]) for row in purpose_rows}
        return {
            "calls_last_24h": int(day_row[0]),
            "prompt_tokens_last_24h": int(day_row[1]),
            "completion_tokens_last_24h": int(day_row[2]),
            "tokens_last_24h": int(day_row[3]),
            "prompt_cache_hit_tokens_last_24h": int(day_row[4]),
            "prompt_cache_miss_tokens_last_24h": int(day_row[5]),
            "calls_with_cache_usage_last_24h": int(day_row[6]),
            "completed_calls_last_7d": int(week_row[0]),
            "ambiguous_review_calls_last_24h": purpose_counts.get(
                "ambiguous_review", 0
            ),
            "confirmed_summary_calls_last_24h": purpose_counts.get(
                "confirmed_summary", 0
            ),
        }

    def _invoke(
        self,
        *,
        evidence: list[dict[str, str]],
        context: dict[str, Any],
        reason: str,
        purpose: str,
    ) -> dict[str, Any]:
        person_opaque = self._opaque(str(context.get("person_key") or "unknown"))
        source_opaque = self._opaque(str(context.get("source_id") or "unknown"))
        payload = self._request_payload(
            evidence=evidence,
            source_kind=str(context.get("source_kind") or "homepage"),
            retrieval_mode=str(context.get("retrieval_mode") or "direct"),
            purpose=purpose,
        )
        request_bytes = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        request_hash = hashlib.sha256(request_bytes).hexdigest()
        token_ceiling = len(request_bytes) + self.settings.max_output_tokens
        reservation_id = self._reserve(
            person_opaque,
            source_opaque,
            reason,
            purpose=purpose,
            token_ceiling=token_ceiling,
        )
        started = time.monotonic()
        failure_usage: dict[str, int] = {}
        try:
            response = self.transport(
                payload,
                {
                    "Authorization": f"Bearer {self._read_key()}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                self.settings.timeout_seconds,
            )
            failure_usage = self._usage(response)
            parsed, usage, response_hash = self._parse_response(
                response, {item["evidence_id"] for item in evidence}
            )
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            self._complete_audit(
                reservation_id,
                status="completed",
                request_hash=request_hash,
                response_hash=response_hash,
                decision=str(parsed["decision"]),
                usage=usage,
                latency_ms=latency_ms,
            )
            return parsed
        except Exception as exc:
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            self._complete_audit(
                reservation_id,
                status="failed",
                request_hash=request_hash,
                response_hash=None,
                decision="uncertain",
                usage=failure_usage,
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
            )
            raise

    def _request_payload(
        self,
        *,
        evidence: list[dict[str, str]],
        source_kind: str,
        retrieval_mode: str,
        purpose: str = "ambiguous_review",
    ) -> dict[str, Any]:
        input_object = {
            "source_kind": source_kind,
            "retrieval_mode": retrieval_mode,
            "evidence": evidence,
        }
        serialized = json.dumps(
            input_object, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(serialized) > self.settings.max_input_chars:
            raise DeepSeekPolicyError("compact delta exceeds input budget")
        if purpose == "confirmed_summary":
            system = (
                "你是人员公开网页已确认变化的摘要器。确定性代码、来源健康、人物绑定、完整度和多轮"
                "确认门均已通过；你不得重新裁决变化是否存在。只能依据 evidence_id 用中文概括具体"
                "新进展，不得补充网页外事实，不得把页面删除推断为离职、撤回或否定事实。只输出 JSON，"
                "decision 必须是 meaningful_change，needs_human_review 必须是 false，并引用实际使用的"
                " evidence_id。"
            )
        else:
            system = (
                "你是人员公开网页变化的二级复核器。确定性代码、来源健康、身份与完整度校验已经先运行。"
                "你只能依据给出的 evidence_id 判断未分类差异，不得补充网页外事实，不得把缺失内容推断为离职、"
                "删除或否定事实。只输出 JSON，不输出 Markdown。每个 meaningful_change 必须引用输入里的"
                " evidence_id；不确定时 decision=uncertain 且 needs_human_review=true。"
            )
        example = (
            {
                "decision": "meaningful_change",
                "summary_zh": "已确认页面新增一项公开进展。",
                "confidence": 0.9,
                "needs_human_review": False,
                "change_types": ["other"],
                "evidence_ids": ["E001"],
            }
            if purpose == "confirmed_summary"
            else {
                "decision": "uncertain",
                "summary_zh": "证据不足，保持待审。",
                "confidence": 0.4,
                "needs_human_review": True,
                "change_types": ["other"],
                "evidence_ids": ["E001"],
            }
        )
        user = (
            "请以 JSON 对象返回，严格使用以下键：decision、summary_zh、confidence、"
            "needs_human_review、change_types、evidence_ids。decision 只能是 "
            "meaningful_change、noise、uncertain。JSON 示例："
            + json.dumps(example, ensure_ascii=False, separators=(",", ":"))
            + "\n输入："
            + serialized
        )
        if len(system) + len(user) > self.settings.max_input_chars:
            raise DeepSeekPolicyError("complete prompt exceeds input budget")
        return {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": self.settings.max_output_tokens,
            "stream": False,
        }

    def _evidence(self, decision: Any) -> list[dict[str, str]]:
        values: list[tuple[str, Any]] = []
        values.extend(("addition", item) for item in getattr(decision, "additions", [])[:8])
        values.extend(("removal", item) for item in getattr(decision, "removals", [])[:8])
        values.extend(
            ("modification", item)
            for item in getattr(decision, "modifications", [])[:8]
        )
        output: list[dict[str, str]] = []
        running = 0
        for index, (operation, value) in enumerate(values, 1):
            sanitized = self._sanitize(value)
            content = json.dumps(
                sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )[:1_000]
            entry = {
                "evidence_id": f"E{index:03d}",
                "operation": operation,
                "content": content,
            }
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            if running + len(encoded) > self.settings.max_input_chars - 2_500:
                break
            output.append(entry)
            running += len(encoded)
        return output

    def _sanitize(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 5:
            return "[TRUNCATED]"
        if isinstance(value, dict):
            return {
                str(key)[:80]: self._sanitize(item, depth=depth + 1)
                for key, item in value.items()
                if not SENSITIVE_KEY.search(str(key))
            }
        if isinstance(value, list):
            return [self._sanitize(item, depth=depth + 1) for item in value[:20]]
        if isinstance(value, str):
            text = SENSITIVE_VALUE.sub("[REDACTED]", value)
            text = re.sub(r"(?is)<script\b.*?</script\s*>", "[SCRIPT_REMOVED]", text)
            return " ".join(text.split())[:800]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:200]

    def _parse_response(
        self, response: dict[str, Any], allowed_evidence: set[str]
    ) -> tuple[dict[str, Any], dict[str, int], str]:
        raw_response = json.dumps(
            response, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        response_hash = hashlib.sha256(raw_response).hexdigest()
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise DeepSeekResponseError("response must contain exactly one choice")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise DeepSeekResponseError("response did not finish with stop")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekResponseError("JSON mode returned empty content")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DeepSeekResponseError("model content is not valid JSON") from exc
        required = {
            "decision",
            "summary_zh",
            "confidence",
            "needs_human_review",
            "change_types",
            "evidence_ids",
        }
        if not isinstance(parsed, dict) or set(parsed) != required:
            raise DeepSeekResponseError("model JSON keys do not match the schema")
        if parsed["decision"] not in ALLOWED_DECISIONS:
            raise DeepSeekResponseError("invalid decision")
        if (
            not isinstance(parsed["summary_zh"], str)
            or not 1 <= len(parsed["summary_zh"]) <= 240
        ):
            raise DeepSeekResponseError("invalid summary_zh")
        if isinstance(parsed["confidence"], bool) or not isinstance(
            parsed["confidence"], (int, float)
        ):
            raise DeepSeekResponseError("invalid confidence")
        if not 0 <= float(parsed["confidence"]) <= 1:
            raise DeepSeekResponseError("confidence is outside [0,1]")
        if not isinstance(parsed["needs_human_review"], bool):
            raise DeepSeekResponseError("invalid needs_human_review")
        if (
            not isinstance(parsed["change_types"], list)
            or not parsed["change_types"]
            or len(parsed["change_types"]) > 8
            or any(item not in ALLOWED_CHANGE_TYPES for item in parsed["change_types"])
        ):
            raise DeepSeekResponseError("invalid change_types")
        if (
            not isinstance(parsed["evidence_ids"], list)
            or len(parsed["evidence_ids"]) > 16
            or any(item not in allowed_evidence for item in parsed["evidence_ids"])
        ):
            raise DeepSeekResponseError("invalid evidence_ids")
        if parsed["decision"] == "meaningful_change" and not parsed["evidence_ids"]:
            raise DeepSeekResponseError("meaningful change requires evidence")
        if (
            parsed["decision"] == "uncertain"
            and parsed["needs_human_review"] is not True
        ):
            raise DeepSeekResponseError("uncertain decision must require human review")
        usage = self._usage(response, strict=True)
        return parsed, usage, response_hash

    @staticmethod
    def _usage(
        response: dict[str, Any], *, strict: bool = False
    ) -> dict[str, int]:
        usage_raw = (
            response.get("usage") if isinstance(response.get("usage"), dict) else {}
        )
        if strict:
            required = ("prompt_tokens", "completion_tokens", "total_tokens")
            if any(
                isinstance(usage_raw.get(key), bool)
                or not isinstance(usage_raw.get(key), int)
                or usage_raw[key] < 0
                for key in required
            ):
                raise DeepSeekResponseError("response usage is missing or invalid")
            if usage_raw["total_tokens"] != (
                usage_raw["prompt_tokens"] + usage_raw["completion_tokens"]
            ):
                raise DeepSeekResponseError("response token totals are inconsistent")
        usage = {
            "prompt_tokens": max(0, int(usage_raw.get("prompt_tokens") or 0)),
            "completion_tokens": max(
                0, int(usage_raw.get("completion_tokens") or 0)
            ),
            "total_tokens": max(0, int(usage_raw.get("total_tokens") or 0)),
        }
        cache_fields_present = all(
            key in usage_raw
            for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        )
        if cache_fields_present:
            cache_values = (
                usage_raw.get("prompt_cache_hit_tokens"),
                usage_raw.get("prompt_cache_miss_tokens"),
            )
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in cache_values
            ):
                raise DeepSeekResponseError("response cache usage is invalid")
            if sum(cache_values) != usage["prompt_tokens"]:
                raise DeepSeekResponseError("response cache token totals are inconsistent")
            usage["prompt_cache_hit_tokens"] = int(cache_values[0])
            usage["prompt_cache_miss_tokens"] = int(cache_values[1])
            usage["cache_usage_reported"] = 1
        else:
            # Some compatible responses omit the optional cache breakdown. Treat
            # all prompt tokens as misses for a conservative cost upper bound.
            usage["prompt_cache_hit_tokens"] = 0
            usage["prompt_cache_miss_tokens"] = usage["prompt_tokens"]
            usage["cache_usage_reported"] = 0
        if usage["total_tokens"] == 0:
            usage["total_tokens"] = (
                usage["prompt_tokens"] + usage["completion_tokens"]
            )
        return usage

    def _read_key(self) -> str:
        assert self.settings.api_key_file is not None
        value = self.settings.api_key_file.read_text(encoding="utf-8").strip()
        if not 20 <= len(value) <= 500 or any(character.isspace() for character in value):
            raise DeepSeekPolicyError("API key file content is invalid")
        return value

    def _prepare_audit_database(self) -> None:
        path = self.settings.audit_database
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS deepseek_audit (
                   reservation_id TEXT PRIMARY KEY,
                   created_at TEXT NOT NULL,
                   person_opaque TEXT NOT NULL,
                   source_opaque TEXT NOT NULL,
                   eligibility_reason TEXT NOT NULL,
                   purpose TEXT NOT NULL DEFAULT 'ambiguous_review',
                   model TEXT NOT NULL,
                   status TEXT NOT NULL,
                   request_hash TEXT,
                   response_hash TEXT,
                   decision TEXT,
                   prompt_tokens INTEGER NOT NULL DEFAULT 0,
                   prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                   prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                   cache_usage_reported INTEGER NOT NULL DEFAULT 0,
                   completion_tokens INTEGER NOT NULL DEFAULT 0,
                   total_tokens INTEGER NOT NULL DEFAULT 0,
                   latency_ms INTEGER,
                   error_type TEXT
                )"""
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(deepseek_audit)")
            }
            if "purpose" not in columns:
                connection.execute(
                    "ALTER TABLE deepseek_audit ADD COLUMN purpose TEXT "
                    "NOT NULL DEFAULT 'ambiguous_review'"
                )
            for name in (
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
                "cache_usage_reported",
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE deepseek_audit ADD COLUMN {name} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
        path.chmod(0o600)

    def _reserve(
        self,
        person_opaque: str,
        source_opaque: str,
        reason: str,
        *,
        purpose: str = "ambiguous_review",
        token_ceiling: int,
    ) -> str:
        now = datetime.now(timezone.utc)
        day = (now - timedelta(days=1)).isoformat()
        week = (now - timedelta(days=7)).isoformat()
        reservation_id = f"dsr_{uuid.uuid4().hex}"
        with sqlite3.connect(
            self.settings.audit_database, timeout=20, isolation_level=None
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            calls_day, tokens_day = connection.execute(
                """SELECT COUNT(*),COALESCE(SUM(total_tokens),0)
                   FROM deepseek_audit
                   WHERE created_at>=?
                     AND status IN ('reserved','completed','failed')""",
                (day,),
            ).fetchone()
            source_calls = connection.execute(
                """SELECT COUNT(*) FROM deepseek_audit
                   WHERE created_at>=? AND source_opaque=?
                     AND status IN ('reserved','completed','failed')""",
                (day, source_opaque),
            ).fetchone()[0]
            person_calls = connection.execute(
                """SELECT COUNT(*) FROM deepseek_audit
                   WHERE created_at>=? AND person_opaque=?
                     AND status IN ('reserved','completed','failed')""",
                (week, person_opaque),
            ).fetchone()[0]
            if calls_day >= self.settings.max_calls_per_day:
                connection.execute("ROLLBACK")
                raise DeepSeekPolicyError("daily call budget exhausted")
            if (
                tokens_day + token_ceiling
                > self.settings.max_total_tokens_per_day
            ):
                connection.execute("ROLLBACK")
                raise DeepSeekPolicyError("daily token budget exhausted")
            if source_calls >= self.settings.max_calls_per_source_per_day:
                connection.execute("ROLLBACK")
                raise DeepSeekPolicyError("per-source daily call budget exhausted")
            if person_calls >= self.settings.max_calls_per_person_per_week:
                connection.execute("ROLLBACK")
                raise DeepSeekPolicyError("per-person weekly call budget exhausted")
            connection.execute(
                """INSERT INTO deepseek_audit(
                   reservation_id,created_at,person_opaque,source_opaque,
                   eligibility_reason,purpose,model,status,total_tokens
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    reservation_id,
                    now.isoformat(),
                    person_opaque,
                    source_opaque,
                    reason,
                    purpose,
                    self.settings.model,
                    "reserved",
                    token_ceiling,
                ),
            )
            connection.execute("COMMIT")
        return reservation_id

    def _complete_audit(
        self,
        reservation_id: str,
        *,
        status: str,
        request_hash: str,
        response_hash: str | None,
        decision: str,
        usage: dict[str, int],
        latency_ms: int,
        error_type: str | None = None,
    ) -> None:
        with sqlite3.connect(self.settings.audit_database) as connection:
            connection.execute(
                """UPDATE deepseek_audit
                   SET status=?,request_hash=?,response_hash=?,decision=?,
                       prompt_tokens=?,prompt_cache_hit_tokens=?,
                       prompt_cache_miss_tokens=?,cache_usage_reported=?,
                       completion_tokens=?,total_tokens=?,
                       latency_ms=?,error_type=?
                   WHERE reservation_id=?""",
                (
                    status,
                    request_hash,
                    response_hash,
                    decision,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("prompt_cache_hit_tokens") or 0),
                    int(usage.get("prompt_cache_miss_tokens") or 0),
                    int(usage.get("cache_usage_reported") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(usage.get("total_tokens") or 0),
                    latency_ms,
                    error_type,
                    reservation_id,
                ),
            )

    def _http_transport(
        self, payload: dict[str, Any], headers: dict[str, str], timeout: int
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.settings.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            raise DeepSeekResponseError(f"DeepSeek HTTP {exc.code}") from exc
        if len(raw) > 2_000_000:
            raise DeepSeekResponseError("DeepSeek response exceeded size limit")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeepSeekResponseError("DeepSeek response is not JSON") from exc
        if not isinstance(value, dict):
            raise DeepSeekResponseError("DeepSeek response is not an object")
        return value

    @staticmethod
    def _opaque(value: str) -> str:
        return "pi_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


__all__ = [
    "ALLOWED_MODELS",
    "DeepSeekDiffReviewer",
    "DeepSeekPolicyError",
    "DeepSeekResponseError",
    "DeepSeekSettings",
]
