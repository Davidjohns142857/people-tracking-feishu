from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from people_intel.connectors import ConnectorExecutor
from people_intel.ledger import LedgerNotFound
from people_intel.schemas import (
    CommandReceipt,
    ConnectorAttempt,
    ConnectorRunRequest,
    ConnectorRunResponse,
    DiscoveryCandidate,
    FetchMethod,
    ScanRun,
    SourceIngestionRequest,
    SourceType,
    utc_now,
)
from people_intel.service import MemoryValidationError, TemporalMemoryService


def execute_connector_run(
    service: TemporalMemoryService,
    executor: ConnectorExecutor,
    request: ConnectorRunRequest,
) -> ConnectorRunResponse:
    """Execute one auditable discovery/fetch run with command idempotency."""
    existing = service.ledger.get_command_receipt(request.command_id)
    if existing is not None:
        if existing.command_type != "connector.run":
            raise MemoryValidationError("command_id has already been used for another command")
        return ConnectorRunResponse.model_validate(existing.result["response"])

    started_at = utc_now()
    output = executor.execute(
        request.channel,
        query=request.query,
        source_uri=request.source_uri,
        max_results=request.max_results,
    )
    completed_at = utc_now()
    run = ScanRun(
        story_id="live-connector-run",
        subject_name=request.subject_name,
        trigger=request.trigger,
        mode="live",
        channels=[request.channel],
        status="completed" if output.status == "completed" else "failed",
        started_at=started_at,
        completed_at=completed_at,
        metadata={
            "command_id": request.command_id,
            "source_uri": request.source_uri,
            "result_count": len(output.results),
            "persist_full_results": request.persist_full_results,
        },
    )
    service.ledger.append_scan_run(run)
    attempt = ConnectorAttempt(
        scan_run_id=run.scan_run_id,
        channel=request.channel,
        route=output.route,
        request_summary=output.request_summary,
        status=output.status,
        duration_ms=output.duration_ms,
        raw_object_ref=output.raw_object_ref,
        error=output.error,
        metadata={
            "transport": "live",
            "result_count": len(output.results),
            "raw_response_authority": True,
            "normalized_projection_only": True,
            **output.metadata,
        },
    )
    service.ledger.append_connector_attempt(attempt)

    source_ids: list[str] = []
    source_version_by_uri: dict[str, str] = {}
    candidates: list[DiscoveryCandidate] = []
    for rank, item in enumerate(output.results, 1):
        uri = str(item.get("uri") or "").strip()
        content = item.get("content")
        persisted = False
        if request.persist_full_results and uri and isinstance(content, str) and content.strip():
            source = service.ingest_source(SourceIngestionRequest(
                source_uri=uri,
                source_type=SourceType(request.channel),
                media_type="application/json",
                content=json.dumps(item, ensure_ascii=False, sort_keys=True),
                normalized_markdown=_normalized_result(item),
                published_at=_parse_datetime(item.get("published_at")),
                retrieved_at=completed_at,
                source_identity=str(item.get("author_handle") or item.get("author") or request.subject_name),
                fetch_method=FetchMethod.QIAOMU if request.channel == "wechat" else FetchMethod.AGENT_REACH,
                metadata={
                    "scan_run_id": run.scan_run_id,
                    "connector_attempt_id": attempt.connector_attempt_id,
                    "connector_route": output.route,
                    "raw_response_ref": output.raw_object_ref,
                    "candidate_rank": rank,
                },
            ))
            source_ids.append(source.source_version_id)
            source_version_by_uri[uri] = source.source_version_id
            persisted = True
        candidate = DiscoveryCandidate(
            scan_run_id=run.scan_run_id,
            channel=request.channel,
            rank=rank,
            title=str(item.get("title") or f"{request.channel} result {rank}"),
            uri=uri or f"urn:people-intel:{request.channel}:{run.scan_run_id}:{rank}",
            published_at=_parse_datetime(item.get("published_at")),
            decision="selected" if persisted else "pending",
            reason_zh=(
                "连接器已取得完整正文，已保存为不可变 SourceDocumentVersion。"
                if persisted
                else "当前仅获得发现摘要；需读取原文后才能进入事实抽取。"
            ),
        )
        service.ledger.append_discovery_candidate(candidate)
        candidates.append(candidate)

    account_tracking: dict[str, Any] = {}
    if output.status == "completed" and request.person_key:
        try:
            from people_intel.person_profiles import PersonProfileService

            plan = PersonProfileService(service).record_platform_observations(
                request.person_key,
                platform=request.channel,
                results=output.results,
                source_version_by_uri=source_version_by_uri,
                query_kind=request.query_kind,
                requested_account_key=request.platform_account_key,
                observed_at=completed_at,
            )
            account_tracking = {
                "search_plan_revision_id": plan.search_plan_revision_id,
                "platform_account_count": len(plan.platform_accounts),
            }
        except (KeyError, ValueError, LedgerNotFound, MemoryValidationError) as exc:
            account_tracking = {"error": str(exc)[:500]}

    response = ConnectorRunResponse(
        command_id=request.command_id,
        scan_run=run,
        attempt=attempt,
        candidates=candidates,
        source_version_ids=source_ids,
        normalized_preview=(output.normalized_text or "")[:12_000] or None,
        metadata={"platform_account_tracking": account_tracking},
    )
    # A failed attempt must remain retryable. Only a successful transport run
    # closes the command idempotently; failures are preserved as ScanRun and
    # ConnectorAttempt records and the durable job state machine schedules the
    # next attempt.
    if output.status == "completed":
        service.ledger.append_command_receipt(CommandReceipt(
            command_id=request.command_id,
            command_type="connector.run",
            status="completed",
            result={"response": response.model_dump(mode="json")},
        ))
    return response


def _normalized_result(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "来源动态")
    author = str(item.get("author") or item.get("author_handle") or "")
    uri = str(item.get("uri") or "")
    published = item.get("published_at") or ""
    content = str(item.get("content") or "")
    return "\n".join([
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"author: {json.dumps(author, ensure_ascii=False)}",
        f"date: {json.dumps(str(published), ensure_ascii=False)}",
        f"url: {json.dumps(uri, ensure_ascii=False)}",
        "---",
        "",
        f"# {title}",
        "",
        content,
    ])


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "" or value == "N/A":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for pattern in ("%Y年%m月%d日 %H:%M", "%Y年%m月%d日", "%Y-%m-%d"):
        try:
            zone = timezone(timedelta(hours=8)) if "年" in pattern else timezone.utc
            return datetime.strptime(text, pattern).replace(tzinfo=zone)
        except ValueError:
            pass
    return None


__all__ = ["execute_connector_run"]
