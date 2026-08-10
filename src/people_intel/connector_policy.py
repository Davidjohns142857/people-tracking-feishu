from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from people_intel.ledger import Ledger
from people_intel.schemas import (
    ConnectorJob,
    ConnectorPolicyDecision,
    ConnectorPolicyDefinition,
)


_DEFAULTS: dict[str, dict[str, object]] = {
    "homepage": {
        "min_interval_seconds": 5,
        "daily_attempt_limit": 1000,
        "circuit_failure_threshold": 4,
        "circuit_cooldown_seconds": 900,
        "serialized": False,
        "rationale_zh": "已知主页优先刷新；限制单域抓取速度，失败只影响对应 URL。",
    },
    "github": {
        "min_interval_seconds": 2,
        "daily_attempt_limit": 1000,
        "circuit_failure_threshold": 5,
        "circuit_cooldown_seconds": 900,
        "serialized": False,
        "rationale_zh": "通过 GitHub CLI/API 读取结构化结果，并受上游 API 配额约束。",
    },
    "arxiv": {
        "min_interval_seconds": 3,
        "daily_attempt_limit": 1000,
        "circuit_failure_threshold": 5,
        "circuit_cooldown_seconds": 600,
        "serialized": False,
        "rationale_zh": "使用 arXiv 公共 API；按人物查询并限制请求间隔。",
    },
    "x": {
        "min_interval_seconds": 15,
        "daily_attempt_limit": 500,
        "circuit_failure_threshold": 3,
        "circuit_cooldown_seconds": 1800,
        "serialized": True,
        "rationale_zh": "共用认证 cookie；本地住宅网络低频读取，关键词搜索失败时转公开发现。",
    },
    "wechat": {
        "min_interval_seconds": 5,
        "daily_attempt_limit": 500,
        "circuit_failure_threshold": 4,
        "circuit_cooldown_seconds": 900,
        "serialized": False,
        "rationale_zh": "搜索与全文读取分离；限制 Playwright 页面创建速率。",
    },
    "xhs": {
        "min_interval_seconds": 90,
        "daily_attempt_limit": 30,
        "circuit_failure_threshold": 2,
        "circuit_cooldown_seconds": 3600,
        "serialized": True,
        "rationale_zh": "持久 Chrome Profile 和短期 xsecToken；严格串行并保守控制每日次数。",
    },
    "news": {
        "min_interval_seconds": 5,
        "daily_attempt_limit": 300,
        "circuit_failure_threshold": 4,
        "circuit_cooldown_seconds": 900,
        "serialized": False,
        "rationale_zh": "授权 IT 桔子 API 优先；公开 Exa 发现受域名白名单约束。",
    },
}


def connector_policy_definitions() -> list[ConnectorPolicyDefinition]:
    values: list[ConnectorPolicyDefinition] = []
    for channel, defaults in _DEFAULTS.items():
        prefix = f"PEOPLE_INTEL_{channel.upper()}_"
        values.append(ConnectorPolicyDefinition(
            channel=channel,
            min_interval_seconds=int(os.environ.get(prefix + "MIN_INTERVAL_SECONDS", defaults["min_interval_seconds"])),
            daily_attempt_limit=int(os.environ.get(prefix + "DAILY_ATTEMPT_LIMIT", defaults["daily_attempt_limit"])),
            circuit_failure_threshold=int(os.environ.get(prefix + "CIRCUIT_FAILURE_THRESHOLD", defaults["circuit_failure_threshold"])),
            circuit_cooldown_seconds=int(os.environ.get(prefix + "CIRCUIT_COOLDOWN_SECONDS", defaults["circuit_cooldown_seconds"])),
            serialized=bool(defaults["serialized"]),
            rationale_zh=str(defaults["rationale_zh"]),
        ))
    return values


class ConnectorExecutionPolicy:
    """Persistent quota and circuit-breaker decision over append-only attempts."""

    def __init__(
        self,
        ledger: Ledger,
        definitions: list[ConnectorPolicyDefinition] | None = None,
        *,
        timezone: str = "Asia/Shanghai",
    ):
        self.ledger = ledger
        self.definitions = {item.channel: item for item in (definitions or connector_policy_definitions())}
        self.timezone = ZoneInfo(timezone)

    @classmethod
    def unrestricted(cls, ledger: Ledger) -> "ConnectorExecutionPolicy":
        return cls(ledger, [
            ConnectorPolicyDefinition(
                channel=channel,
                min_interval_seconds=0,
                daily_attempt_limit=1_000_000,
                circuit_failure_threshold=1_000_000,
                circuit_cooldown_seconds=1,
                serialized=False,
                rationale_zh="test-only unrestricted policy",
            )
            for channel in _DEFAULTS
        ])

    def evaluate(self, job: ConnectorJob, now: datetime) -> ConnectorPolicyDecision:
        definition = self.definitions[job.request.channel]
        attempts = sorted(
            (item for item in self.ledger.list_connector_attempts() if item.channel == job.request.channel),
            key=lambda item: item.created_at,
        )
        if not attempts:
            return self._ready()

        local_now = now.astimezone(self.timezone)
        start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily = [item for item in attempts if item.created_at.astimezone(self.timezone) >= start_of_day]
        if len(daily) >= definition.daily_attempt_limit:
            tomorrow = start_of_day + timedelta(days=1)
            return ConnectorPolicyDecision(
                allowed=False,
                reason="daily_limit",
                next_attempt_at=tomorrow,
                detail_zh=f"{job.request.channel} 今日真实访问已达 {definition.daily_attempt_limit} 次，延迟到下一自然日。",
            )

        consecutive_failures = 0
        for attempt in reversed(attempts):
            if attempt.status == "completed":
                break
            if attempt.status in {"failed", "unconfigured"}:
                consecutive_failures += 1
        latest = attempts[-1]
        if consecutive_failures >= definition.circuit_failure_threshold:
            retry_at = latest.created_at + timedelta(seconds=definition.circuit_cooldown_seconds)
            if retry_at > now:
                return ConnectorPolicyDecision(
                    allowed=False,
                    reason="circuit_open",
                    next_attempt_at=retry_at,
                    detail_zh=f"连续 {consecutive_failures} 次失败，熔断至 {retry_at.isoformat()}，等待认证或站点恢复。",
                )

        interval_at = latest.created_at + timedelta(seconds=definition.min_interval_seconds)
        if interval_at > now:
            return ConnectorPolicyDecision(
                allowed=False,
                reason="minimum_interval",
                next_attempt_at=interval_at,
                detail_zh=f"距上次 {job.request.channel} 访问过近，按渠道最小间隔延迟执行。",
            )
        return self._ready()

    @staticmethod
    def _ready() -> ConnectorPolicyDecision:
        return ConnectorPolicyDecision(
            allowed=True,
            reason="ready",
            detail_zh="渠道配额、最小间隔与熔断状态均允许执行。",
        )


__all__ = ["ConnectorExecutionPolicy", "connector_policy_definitions"]
