from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from people_intel.connector_jobs import ConnectorJobQueue
from people_intel.schemas import (
    ConnectorJobEnqueueRequest,
    ConnectorRunRequest,
    ConnectorScheduleConfig,
    ConnectorScheduleRunResult,
    ConnectorScheduleSubscription,
    utc_now,
)
from people_intel.service import TemporalMemoryService


def load_connector_schedule(path: str | Path) -> ConnectorScheduleConfig:
    return ConnectorScheduleConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


def merge_person_profile_subscriptions(
    service: TemporalMemoryService,
    config: ConnectorScheduleConfig,
) -> ConnectorScheduleConfig:
    """Add the current person-first SearchPlans to the durable cron contract.

    Search plans remain the authority for per-person targets. This function is
    a rebuildable scheduler projection: it never edits a profile or source.
    """
    from people_intel.person_profiles import PersonProfileService, stable_id

    subscriptions = {item.subscription_id: item for item in config.subscriptions}
    profiles = PersonProfileService(service)
    for summary in profiles.list_profiles():
        plan = profiles.latest_search_plan(summary.person_key)
        for target in plan.targets:
            if target.channel == "other":
                continue
            subscription_id = stable_id("ppsub", summary.person_key, target.target_id)
            subscriptions[subscription_id] = ConnectorScheduleSubscription(
                subscription_id=subscription_id,
                subject_entity_id=summary.entity_id,
                subject_name=summary.canonical_name,
                channel=target.channel,
                query=target.value,
                source_uri=target.value if target.target_type == "known_url" else None,
                interval_minutes=1440 if target.cadence == "daily" else 10080,
                max_results=5,
                max_attempts=2 if target.channel == "xhs" else 3,
                enabled=True,
                person_key=summary.person_key,
                query_kind=target.query_kind,
                platform_account_key=target.platform_account_key,
            )
    return config.model_copy(update={"subscriptions": list(subscriptions.values())})


class ConnectorScheduleProducer:
    """Turn stable tracked-source subscriptions into idempotent durable jobs."""

    def __init__(self, service: TemporalMemoryService, config: ConnectorScheduleConfig):
        self.service = service
        self.config = config
        self.timezone = ZoneInfo(config.timezone)

    def run_once(self, *, now: datetime | None = None, dry_run: bool = False) -> ConnectorScheduleRunResult:
        evaluated_at = now or utc_now()
        jobs = self.service.ledger.list_connector_jobs()
        due: list[ConnectorScheduleSubscription] = []
        disabled_count = 0
        for subscription in self.config.subscriptions:
            if not subscription.enabled:
                disabled_count += 1
                continue
            matching = [
                item for item in jobs
                if item.request.tracking_key == subscription.subscription_id
            ]
            latest = max(matching, key=lambda item: item.created_at) if matching else None
            interval = timedelta(minutes=subscription.interval_minutes)
            if latest is None or latest.created_at + interval <= evaluated_at:
                due.append(subscription)

        enqueued_count = 0
        existing_count = 0
        connector_job_ids: list[str] = []
        if not dry_run:
            queue = ConnectorJobQueue(self.service)
            for subscription in due:
                command_id = self._command_id(subscription, evaluated_at)
                existed = self.service.ledger.find_connector_job_by_command_id(command_id) is not None
                view = queue.enqueue(ConnectorJobEnqueueRequest(
                    request=ConnectorRunRequest(
                        command_id=command_id,
                        channel=subscription.channel,
                        query=subscription.query,
                        source_uri=subscription.source_uri,
                        subject_name=subscription.subject_name,
                        trigger="cron",
                        max_results=subscription.max_results,
                        tracking_key=subscription.subscription_id,
                        person_key=subscription.person_key,
                        query_kind=subscription.query_kind,
                        platform_account_key=subscription.platform_account_key,
                    ),
                    max_attempts=subscription.max_attempts,
                    not_before=evaluated_at,
                ))
                connector_job_ids.append(view.job.connector_job_id)
                if existed:
                    existing_count += 1
                else:
                    enqueued_count += 1

        return ConnectorScheduleRunResult(
            evaluated_at=evaluated_at,
            subscription_count=len(self.config.subscriptions),
            due_count=len(due),
            enqueued_count=enqueued_count,
            existing_count=existing_count,
            disabled_count=disabled_count,
            connector_job_ids=connector_job_ids,
        )

    @staticmethod
    def _command_id(subscription: ConnectorScheduleSubscription, now: datetime) -> str:
        interval_seconds = subscription.interval_minutes * 60
        bucket = int(now.timestamp()) // interval_seconds
        digest = hashlib.sha256(f"{subscription.subscription_id}:{bucket}".encode()).hexdigest()[:28]
        return f"cmd_cron_{digest}"


__all__ = ["ConnectorScheduleProducer", "load_connector_schedule"]
