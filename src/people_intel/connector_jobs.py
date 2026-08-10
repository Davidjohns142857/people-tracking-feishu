from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from people_intel.connector_runtime import execute_connector_run
from people_intel.connectors import ConnectorExecutor
from people_intel.connector_policy import ConnectorExecutionPolicy
from people_intel.ledger import Ledger, LedgerConflict
from people_intel.schemas import (
    ConnectorJob,
    ConnectorJobEnqueueRequest,
    ConnectorJobEvent,
    ConnectorJobView,
    CommandEnvelope,
    ConnectorRunRequest,
    utc_now,
)
from people_intel.service import TemporalMemoryService


def connector_job_view(ledger: Ledger, job: ConnectorJob) -> ConnectorJobView:
    events = ledger.list_connector_job_events(job.connector_job_id)
    last = events[-1] if events else None
    if last is None or last.event_type == "queued":
        status = "queued"
    elif last.event_type == "claimed":
        status = "running"
    elif last.event_type in {"retry_scheduled", "deferred"}:
        status = "retry_wait"
    else:
        status = last.event_type
    return ConnectorJobView(
        job=job,
        status=status,
        attempt_count=sum(event.event_type == "claimed" for event in events),
        last_event=last,
        events=events,
    )


class ConnectorJobQueue:
    """Append-only connector queue facade.

    The immutable job describes intent. Every state transition is a separate
    ``ConnectorJobEvent`` so crashes and retries remain inspectable.
    """

    def __init__(self, service: TemporalMemoryService):
        self.service = service

    def enqueue(self, payload: ConnectorJobEnqueueRequest) -> ConnectorJobView:
        existing = self.service.ledger.find_connector_job_by_command_id(payload.request.command_id)
        if existing is not None:
            return connector_job_view(self.service.ledger, existing)
        job = ConnectorJob(
            command_id=payload.request.command_id,
            request=payload.request,
            max_attempts=payload.max_attempts,
            not_before=payload.not_before,
        )
        try:
            self.service.ledger.append_connector_job(job)
        except LedgerConflict:
            # A concurrent API/Feishu delivery may have inserted the same
            # command after our first lookup. The unique command_id is the
            # arbitration point; return the winner instead of duplicating work.
            winner = self.service.ledger.find_connector_job_by_command_id(payload.request.command_id)
            if winner is None:
                raise
            return connector_job_view(self.service.ledger, winner)
        self.service.ledger.append_connector_job_event(ConnectorJobEvent(
            connector_job_id=job.connector_job_id,
            event_type="queued",
        ))
        return connector_job_view(self.service.ledger, job)

    def get(self, connector_job_id: str) -> ConnectorJobView:
        return connector_job_view(
            self.service.ledger,
            self.service.ledger.get_connector_job(connector_job_id),
        )

    def list(self) -> list[ConnectorJobView]:
        return [connector_job_view(self.service.ledger, item) for item in self.service.ledger.list_connector_jobs()]


def build_scan_request_handler(
    service: TemporalMemoryService,
    *,
    publish: Callable[[str, str, str, dict[str, Any]], None] | None = None,
) -> Callable[[CommandEnvelope], tuple[dict[str, Any], str]]:
    """Build the fast ingress handler shared by API and Feishu transports.

    This function only appends a durable ``ConnectorJob``. It never calls a
    connector, browser or external platform, keeping Feishu's event callback
    comfortably inside its acknowledgement deadline.
    """

    def enqueue_scan_command(command: CommandEnvelope) -> tuple[dict[str, Any], str]:
        if "channel" not in command.payload or "query" not in command.payload:
            return {
                "scan_request_id": f"scan_{command.command_id}",
                "entity_ids": command.payload.get("entity_ids", []),
                "scan_kind": command.payload.get("scan_kind", "incremental"),
            }, "accepted"
        connector_request = ConnectorRunRequest(
            command_id=f"{command.command_id}:connector",
            channel=command.payload["channel"],
            query=command.payload["query"],
            source_uri=command.payload.get("source_uri"),
            subject_name=command.payload.get("subject_name", command.payload["query"]),
            trigger="feishu",
            max_results=command.payload.get("max_results", 5),
        )
        view = ConnectorJobQueue(service).enqueue(ConnectorJobEnqueueRequest(request=connector_request))
        if publish is not None:
            publish(
                "connector.job.queued",
                "connector_job",
                view.job.connector_job_id,
                {"channel": connector_request.channel, "command_id": connector_request.command_id},
            )
        return {
            "connector_job_id": view.job.connector_job_id,
            "status": view.status,
            "channel": connector_request.channel,
            "query": connector_request.query,
        }, "accepted"

    return enqueue_scan_command


@dataclass(frozen=True)
class ConnectorWorkerConfig:
    worker_id: str
    lease_seconds: int = 180
    base_backoff_seconds: int = 60
    max_backoff_seconds: int = 3600


class ConnectorWorker:
    """Claims one durable job and records success, retry or dead-letter state."""

    def __init__(
        self,
        service: TemporalMemoryService,
        executor: ConnectorExecutor,
        config: ConnectorWorkerConfig,
        policy: ConnectorExecutionPolicy | None = None,
    ):
        self.service = service
        self.executor = executor
        self.config = config
        self.policy = policy or ConnectorExecutionPolicy(service.ledger)

    def run_once(self) -> ConnectorJobView | None:
        now = utc_now()
        claimed = self.service.ledger.claim_next_connector_job(
            self.config.worker_id,
            self.config.lease_seconds,
            now,
        )
        if claimed is None:
            return None
        job, claim_event = claimed
        attempt = claim_event.attempt
        # A recovered lease may produce a claim beyond the immutable job's
        # attempt budget. Terminalize it before consulting channel throttles;
        # otherwise a quota/circuit deferral could keep an exhausted job alive
        # indefinitely. A deferred claim at exactly max_attempts is still
        # allowed to execute because deferral itself did not access a source.
        if attempt > job.max_attempts:
            self._append_terminal(job, attempt, "重试次数已耗尽。")
            return connector_job_view(self.service.ledger, job)
        policy = self.policy.evaluate(job, now)
        if not policy.allowed:
            self.service.ledger.append_connector_job_event(ConnectorJobEvent(
                connector_job_id=job.connector_job_id,
                event_type="deferred",
                worker_id=self.config.worker_id,
                attempt=attempt,
                next_attempt_at=policy.next_attempt_at,
                error=policy.detail_zh,
                result={"policy_reason": policy.reason},
            ))
            return connector_job_view(self.service.ledger, job)
        try:
            response = execute_connector_run(self.service, self.executor, job.request)
            if response.attempt.status == "completed":
                self.service.ledger.append_connector_job_event(ConnectorJobEvent(
                    connector_job_id=job.connector_job_id,
                    event_type="succeeded",
                    worker_id=self.config.worker_id,
                    attempt=attempt,
                    result={
                        "scan_run_id": response.scan_run.scan_run_id,
                        "connector_attempt_id": response.attempt.connector_attempt_id,
                        "source_version_ids": response.source_version_ids,
                        "candidate_count": len(response.candidates),
                    },
                ))
            else:
                self._retry_or_dead_letter(job, attempt, response.attempt.error or response.attempt.status)
        except Exception as exc:  # execution errors are durable job outcomes
            self._retry_or_dead_letter(job, attempt, f"{type(exc).__name__}: {exc}")
        return connector_job_view(self.service.ledger, job)

    def _retry_or_dead_letter(self, job: ConnectorJob, attempt: int, error: str) -> None:
        if attempt >= job.max_attempts:
            self._append_terminal(job, attempt, error)
            return
        delay = min(
            self.config.max_backoff_seconds,
            self.config.base_backoff_seconds * (2 ** max(0, attempt - 1)),
        )
        self.service.ledger.append_connector_job_event(ConnectorJobEvent(
            connector_job_id=job.connector_job_id,
            event_type="retry_scheduled",
            worker_id=self.config.worker_id,
            attempt=attempt,
            next_attempt_at=utc_now() + timedelta(seconds=delay),
            error=error,
        ))

    def _append_terminal(self, job: ConnectorJob, attempt: int, error: str) -> None:
        self.service.ledger.append_connector_job_event(ConnectorJobEvent(
            connector_job_id=job.connector_job_id,
            event_type="dead_lettered",
            worker_id=self.config.worker_id,
            attempt=attempt,
            error=error,
        ))


__all__ = [
    "ConnectorJobQueue",
    "ConnectorWorker",
    "ConnectorWorkerConfig",
    "build_scan_request_handler",
    "connector_job_view",
]
