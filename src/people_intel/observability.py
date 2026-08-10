from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from people_intel.schemas import CapabilityDefinition, CollectionPage, ComponentStatus
from people_intel.feishu_runtime import FeishuRuntimeConfig
from people_intel.service import TemporalMemoryService


T = TypeVar("T")


CAPABILITIES = [
    CapabilityDefinition(
        capability_id="immutable-sources",
        name="Immutable source versioning",
        status="implemented",
        endpoint_paths=["/v1/source-documents", "/v1/sources"],
        demo_step_ids=["source-v1", "source-dedup", "source-v2"],
        test_refs=["test_content_and_sources.py"],
    ),
    CapabilityDefinition(
        capability_id="bitemporal-memory",
        name="Bitemporal assertions and working views",
        status="implemented",
        endpoint_paths=["/v1/assertions", "/v1/graph/query"],
        demo_step_ids=["assertion", "time-machine", "conflict"],
        test_refs=["test_temporal_memory.py"],
    ),
    CapabilityDefinition(
        capability_id="human-governance",
        name="Append-only human annotations",
        status="implemented",
        endpoint_paths=["/v1/annotations"],
        demo_step_ids=["annotation", "derived-stale"],
        test_refs=["test_temporal_memory.py"],
    ),
    CapabilityDefinition(
        capability_id="initial-profile-review",
        name="Human-gated initial profile import and inference review",
        status="implemented",
        endpoint_paths=[
            "/v1/initial-knowledge/imports/icml-markdown",
            "/v1/initial-reviews",
            "/v1/initial-reviews/{review_batch_id}/document",
            "/v1/initial-reviews/{review_batch_id}/bulk-decisions",
            "/v1/initial-reviews/{review_batch_id}/delivery",
            "/v1/initial-reviews/{review_batch_id}/messages",
            "/v1/initial-review-items/{review_item_id}/decisions",
        ],
        demo_step_ids=["initial-review-batch", "initial-review-decision", "derived-inference-review"],
        test_refs=["test_initial_review.py", "test_initial_knowledge_import.py", "test_consolidated_review_and_source_watch.py"],
    ),
    CapabilityDefinition(
        capability_id="dossier-source-watch",
        name="Versioned directory and Feishu dossier tracking",
        status="implemented",
        endpoint_paths=["/v1/source-watches/directory/scan", "/v1/source-watches/feishu/scan"],
        demo_step_ids=["directory-manifest", "feishu-document-version"],
        test_refs=["test_consolidated_review_and_source_watch.py"],
    ),
    CapabilityDefinition(
        capability_id="live-source-connectors",
        name="Auditable X, WeChat, Xiaohongshu and funding-source connector runs",
        status="implemented",
        endpoint_paths=[
            "/v1/system/connectors",
            "/v1/system/connector-policy",
            "/v1/system/connector-schedule",
            "/v1/connectors/runs",
            "/v1/connector-jobs",
            "/v1/connector-jobs/{connector_job_id}",
            "/v1/connector-jobs/{connector_job_id}/events",
            "/v1/scan-runs/{scan_run_id}/attempts",
        ],
        demo_step_ids=["connector.job.queued", "connector.started", "connector.completed", "source.versioned"],
        test_refs=["test_live_source_connectors.py", "test_connector_jobs.py", "test_connector_policy_scheduler.py", "test_feishu_runtime.py"],
    ),
    CapabilityDefinition(
        capability_id="cognee-temporal-projection",
        name="Cognee temporal graph and vector projection",
        status="implemented",
        endpoint_paths=["/v1/cognee/status", "/v1/source-documents/{source_version_id}/cognify", "/v1/cognee/project-all", "/v1/cognee/recall"],
        demo_step_ids=["cognee-projection", "cognee-recall"],
        test_refs=["test_cognee_projection.py"],
    ),
    CapabilityDefinition(
        capability_id="person-exact-profile-baseline",
        name="Person-first exact profile baseline and degradation path",
        status="implemented",
        endpoint_paths=[
            "/v1/person-profile-import-runs",
            "/v1/person-profiles",
            "/v1/person-profiles/{person_key}",
            "/v1/person-profiles/{person_key}/search-plan",
            "/v1/person-profiles/{person_key}/platform-accounts",
            "/v1/person-profiles/{person_key}/scan-runs",
            "/v1/person-update-bundles/{bundle_id}/export.md",
            "/v1/person-profiles/{person_key}/cognee/project",
        ],
        demo_step_ids=["person-slice", "person-delta-bundle", "person-profile-revision"],
        test_refs=["test_person_profiles.py"],
    ),
    CapabilityDefinition(
        capability_id="identity-clusters",
        name="Reversible identity clusters",
        status="implemented",
        endpoint_paths=["/v1/commands"],
        demo_step_ids=["identity"],
        test_refs=["test_identity_and_import.py"],
    ),
    CapabilityDefinition(
        capability_id="graph-signals",
        name="Evidence-linked graph signals",
        status="implemented",
        endpoint_paths=["/v1/signals"],
        demo_step_ids=["signal-snapshot"],
        test_refs=["test_scheduler_signals_commands.py"],
    ),
]


def collection_page(
    items: Iterable[T],
    *,
    offset: int,
    limit: int,
    query: str | None = None,
    search_value: Callable[[T], str] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
) -> CollectionPage[T]:
    values = list(items)
    if query and search_value:
        needle = query.casefold()
        values = [item for item in values if needle in search_value(item).casefold()]
    if sort_by:
        def key(item: T):
            value: Any = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for part in sort_by.split("."):
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(part)
            return (value is None, str(value) if value is not None else "")

        values.sort(key=key, reverse=descending)
    total = len(values)
    page = values[offset : offset + limit]
    next_offset = offset + limit if offset + limit < total else None
    return CollectionPage(items=page, total=total, offset=offset, limit=limit, next_offset=next_offset)


def ledger_counts(service: TemporalMemoryService) -> dict[str, int]:
    ledger = service.ledger
    return {
        "sources": len(ledger.list_source_versions()),
        "episodes": len(ledger.list_episodes()),
        "spans": len(ledger.list_evidence_spans()),
        "entities": len(ledger.list_entities()),
        "runs": len(ledger.list_extraction_runs()),
        "initial_review_batches": len(ledger.list_initial_review_batches()),
        "initial_review_items": len(ledger.list_initial_review_items()),
        "initial_review_decisions": len(ledger.list_initial_review_decisions()),
        "assertions": len(ledger.list_assertions()),
        "relations": len(ledger.list_assertion_relations()),
        "annotations": len(ledger.list_annotations()),
        "derived": len(ledger.list_derived_inputs()),
        "signals": len(ledger.list_signals()),
        "candidates": len(ledger.list_candidates()),
        "scan_runs": len(ledger.list_scan_runs()),
        "connector_attempts": len(ledger.list_connector_attempts()),
        "discovery_candidates": len(ledger.list_discovery_candidates()),
        "change_sets": len(ledger.list_change_sets()),
        "workflow_artifacts": len(ledger.list_workflow_artifacts()),
        "connector_jobs": len(ledger.list_connector_jobs()),
        "connector_job_events": len(ledger.list_connector_job_events()),
        "person_keys": len(ledger.list_person_keys()),
        "person_source_slices": len(ledger.list_person_source_slices()),
        "person_profile_revisions": len(ledger.list_person_profile_revisions()),
        "person_search_plan_revisions": len(ledger.list_person_search_plan_revisions()),
        "person_profile_scan_runs": len(ledger.list_person_profile_scan_runs()),
        "person_update_bundles": len(ledger.list_person_update_bundles()),
        "person_digest_batches": len(ledger.list_person_digest_batches()),
        "profile_patch_review_items": len(ledger.list_profile_patch_review_items()),
        "person_cognee_projections": len(ledger.list_person_cognee_projections()),
    }


def ledger_metrics(service: TemporalMemoryService) -> dict[str, int | float | str | None]:
    assertions = service.ledger.list_assertions()
    evidenced = sum(bool(item.evidence_span_ids) for item in assertions)
    pending = sum(bool(item.review_required) for item in assertions)
    conflicted = sum(str(item.status) == "conflicted" for item in assertions)
    coverage = round(evidenced / len(assertions) * 100, 1) if assertions else 0.0
    return {
        "evidence_coverage_percent": coverage,
        "review_required": pending,
        "initially_conflicted": conflicted,
        "working_view_threshold": service.working_view_threshold,
        "working_policy_version": service.working_policy_version,
    }


def component_statuses(service: TemporalMemoryService, live_available: bool) -> list[ComponentStatus]:
    qiaomu_candidates = [
        Path.home() / "miniclaw/.agents/skills/qiaomu-markdown-proxy/scripts/fetch.sh",
        Path.home() / ".codex/skills/qiaomu-markdown-proxy/scripts/fetch.sh",
    ]
    cognee_runtime = service.cognee_status()
    feishu_config = FeishuRuntimeConfig.from_environment()
    feishu_missing = feishu_config.missing()
    feishu_ready = feishu_config.ready
    return [
        ComponentStatus(
            component_id="object-store",
            name="Content-addressed object store",
            layer="authority",
            status="ready",
            authoritative=True,
            detail=str(service.object_store.root),
        ),
        ComponentStatus(
            component_id="ledger",
            name=type(service.ledger).__name__,
            layer="authority",
            status="ready",
            authoritative=True,
            detail="Append-only manifest, assertion and annotation ledger",
        ),
        ComponentStatus(
            component_id="neo4j",
            name="Neo4j projection",
            layer="projection",
            status="unconfigured" if not os.environ.get("PEOPLE_INTEL_NEO4J_URI") else "ready",
            detail="Disposable graph projection",
        ),
        ComponentStatus(
            component_id="cognee",
            name=f"Cognee {cognee_runtime.package_version or ''}".strip(),
            layer="projection",
            status=cognee_runtime.status,
            detail=(
                f"{cognee_runtime.detail_zh} provider={cognee_runtime.llm_provider}; "
                f"model={cognee_runtime.llm_model}; dataset={cognee_runtime.dataset_name}; "
                "authority=projection_only"
            ),
        ),
        ComponentStatus(
            component_id="qiaomu",
            name="qiaomu markdown proxy",
            layer="connector",
            status="ready" if any(path.exists() for path in qiaomu_candidates) else "unavailable",
            detail="Known-URL normalization scripts",
        ),
        ComponentStatus(
            component_id="feishu",
            name="Feishu interface",
            layer="interface",
            status="ready" if feishu_ready else "unconfigured",
            detail=(
                "Official WebSocket transport is configured with App credentials and actor/chat allowlist; "
                "callback only authenticates and enqueues durable commands"
                if feishu_ready
                else "WebSocket transport; missing deployment settings: " + ", ".join(feishu_missing)
            ),
        ),
        ComponentStatus(
            component_id="live-ledger",
            name="Live PostgreSQL view",
            layer="runtime",
            status="ready" if live_available else "unconfigured",
            detail="Read-only from the demo cockpit",
        ),
    ]


def json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def latest_snapshot_hash(project_root: Path) -> str | None:
    manifests = sorted(project_root.glob(".people_intel/snapshots/*/manifest.json"))
    if not manifests:
        return None
    try:
        return json.loads(manifests[-1].read_text(encoding="utf-8")).get("root_hash")
    except (OSError, json.JSONDecodeError):
        return None


def latest_verification(project_root: Path) -> dict[str, Any] | None:
    path = project_root / ".people_intel" / "demo-verification.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


__all__ = [
    "CAPABILITIES",
    "collection_page",
    "component_statuses",
    "json_hash",
    "latest_verification",
    "latest_snapshot_hash",
    "ledger_counts",
    "ledger_metrics",
]
