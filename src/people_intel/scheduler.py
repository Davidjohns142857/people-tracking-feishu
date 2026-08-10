from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class ScanTier(StrEnum):
    A = "A"
    B = "B"
    C = "C"


@dataclass(frozen=True)
class TrackedEntity:
    entity_id: str
    tier: ScanTier = ScanTier.B
    active: bool = True
    last_incremental_scan_at: datetime | None = None
    last_full_scan_at: datetime | None = None
    source_channels: tuple[str, ...] = (
        "github",
        "arxiv",
        "homepage",
        "x",
        "wechat",
        "xhs",
        "news",
    )


@dataclass(frozen=True)
class Cohort:
    cohort_id: str
    name: str
    entity_ids: frozenset[str]
    top_list: bool = False


@dataclass(frozen=True)
class ScanRequest:
    entity_id: str
    scan_kind: str
    channels: tuple[str, ...]
    reason: str
    due_at: datetime


@dataclass(frozen=True)
class DeliveryRequest:
    delivery_kind: str
    due_at: datetime
    reason: str


@dataclass(frozen=True)
class SchedulePlan:
    scans: tuple[ScanRequest, ...] = field(default_factory=tuple)
    deliveries: tuple[DeliveryRequest, ...] = field(default_factory=tuple)


class MonitoringScheduler:
    """Pure scheduling policy; orchestration and connector execution stay external."""

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.timezone = ZoneInfo(timezone)

    def plan(
        self,
        *,
        now: datetime,
        tracked: list[TrackedEntity],
        cohorts: list[Cohort] | None = None,
    ) -> SchedulePlan:
        local_now = now.astimezone(self.timezone)
        top_list_ids = {
            entity_id
            for cohort in (cohorts or [])
            if cohort.top_list
            for entity_id in cohort.entity_ids
        }
        scans: list[ScanRequest] = []
        for item in tracked:
            if not item.active:
                continue
            tier = ScanTier.A if item.entity_id in top_list_ids else item.tier
            incremental_period = {
                ScanTier.A: timedelta(days=1),
                ScanTier.B: timedelta(days=3),
                ScanTier.C: timedelta(days=7),
            }[tier]
            if self._is_due(item.last_incremental_scan_at, local_now, incremental_period):
                scans.append(
                    ScanRequest(
                        entity_id=item.entity_id,
                        scan_kind="incremental",
                        channels=item.source_channels,
                        reason=f"tier_{tier}_incremental",
                        due_at=local_now,
                    )
                )
            if self._is_due(item.last_full_scan_at, local_now, timedelta(days=7)):
                scans.append(
                    ScanRequest(
                        entity_id=item.entity_id,
                        scan_kind="full",
                        channels=item.source_channels,
                        reason="weekly_full_coverage",
                        due_at=local_now,
                    )
                )

        deliveries: list[DeliveryRequest] = []
        daily = datetime.combine(local_now.date(), time(18, 0), self.timezone)
        if daily <= local_now < daily + timedelta(minutes=5):
            deliveries.append(DeliveryRequest("daily_high_score_signals", daily, "daily_18_00"))
        weekly = datetime.combine(local_now.date(), time(8, 30), self.timezone)
        if local_now.weekday() == 0 and weekly <= local_now < weekly + timedelta(minutes=5):
            deliveries.append(DeliveryRequest("weekly_graph_digest", weekly, "monday_08_30"))
        return SchedulePlan(tuple(scans), tuple(deliveries))

    def next_delivery_times(self, now: datetime) -> dict[str, datetime]:
        local_now = now.astimezone(self.timezone)
        daily = datetime.combine(local_now.date(), time(18, 0), self.timezone)
        if daily <= local_now:
            daily += timedelta(days=1)
        days_until_monday = (7 - local_now.weekday()) % 7
        weekly_date = local_now.date() + timedelta(days=days_until_monday)
        weekly = datetime.combine(weekly_date, time(8, 30), self.timezone)
        if weekly <= local_now:
            weekly += timedelta(days=7)
        return {"daily_high_score_signals": daily, "weekly_graph_digest": weekly}

    @staticmethod
    def _is_due(previous: datetime | None, now: datetime, period: timedelta) -> bool:
        if previous is None:
            return True
        return previous.astimezone(now.tzinfo) + period <= now


__all__ = [
    "Cohort",
    "DeliveryRequest",
    "MonitoringScheduler",
    "ScanRequest",
    "ScanTier",
    "SchedulePlan",
    "TrackedEntity",
]
