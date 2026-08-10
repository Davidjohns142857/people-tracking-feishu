from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from people_intel.schemas import StrictModel, new_id, utc_now


PROFILE_SCHEMA_VERSION = "person-profile-v1"
PROFILE_FIELDS = (
    "identity",
    "urls",
    "source_documents",
    "affiliations",
    "education",
    "awards",
    "papers",
    "projects",
    "research_topics",
    "career_and_funding",
    "relationships",
    "supplementary_information",
)


class PersonKeyRecord(StrictModel):
    person_key_id: str = Field(default_factory=lambda: new_id("pkey"))
    person_key: str
    entity_id: str
    canonical_name: str
    collision_discriminator: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class PersonSourceSlice(StrictModel):
    person_source_slice_id: str = Field(default_factory=lambda: new_id("pslice"))
    person_key: str
    entity_id: str
    source_version_id: str
    evidence_span_id: str
    assignment_method: Literal["assertion_evidence", "structured_record", "human"]
    confidence: float = Field(ge=0.0, le=1.0)
    quote_hash: str
    created_at: datetime = Field(default_factory=utc_now)


class ProfileEvidenceRef(StrictModel):
    source_version_id: str
    evidence_span_id: str
    source_uri: str
    source_type: str
    quote: str
    content_hash: str
    retrieved_at: datetime
    published_at: datetime | None = None


class ProfileFieldItem(StrictModel):
    field_item_id: str
    field_name: str
    label_zh: str
    value: Any
    assertion_ids: list[str] = Field(default_factory=list)
    evidence: list[ProfileEvidenceRef] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    review_status: Literal["accepted", "pending", "rejected", "conflicted"]
    extraction_method: Literal[
        "deterministic_assertion_projection",
        "deterministic_claim_classifier",
        "derived_rule",
        "human_correction",
    ]
    valid_time: dict[str, Any] = Field(default_factory=dict)
    known_at: datetime
    supersedes_field_item_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonProfileRevision(StrictModel):
    profile_revision_id: str = Field(default_factory=lambda: new_id("prev"))
    person_key: str
    entity_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    schema_version: Literal["person-profile-v1"] = PROFILE_SCHEMA_VERSION
    revision_number: int = Field(ge=1)
    supersedes_revision_id: str | None = None
    fields: dict[str, list[ProfileFieldItem]] = Field(default_factory=dict)
    missing_fields: dict[str, str] = Field(default_factory=dict)
    source_slice_ids: list[str] = Field(default_factory=list)
    assertion_ids: list[str] = Field(default_factory=list)
    coverage_percent: float = Field(ge=0.0, le=100.0)
    accepted_item_count: int = 0
    pending_item_count: int = 0
    conflicted_item_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonSearchTarget(StrictModel):
    target_id: str
    target_type: Literal["known_url", "channel_query"]
    channel: Literal["homepage", "github", "arxiv", "x", "xhs", "wechat", "news", "other"]
    value: str
    route: str
    cadence: Literal["daily", "weekly"]
    enabled: bool = True
    priority: int = Field(ge=1)
    reason_zh: str
    fallback_route: str | None = None
    query_kind: Literal[
        "known_account_feed",
        "known_url_refresh",
        "account_discovery",
        "person_mentions",
        "repository_discovery",
        "publication_discovery",
        "event_discovery",
    ] = "event_discovery"
    platform_account_key: str | None = None
    capture_fields: list[str] = Field(default_factory=list)
    checkpoint_fields: list[str] = Field(default_factory=list)
    identity_gate_zh: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonPlatformAccount(StrictModel):
    """Versioned first-party account projection embedded in a SearchPlan."""

    platform_account_key: str
    person_key: str
    entity_id: str
    platform: Literal["x", "xhs", "github"]
    username: str | None = None
    platform_user_id: str | None = None
    profile_url: str
    display_name: str | None = None
    identity_status: Literal["candidate", "pending_review", "confirmed", "rejected", "conflicted"]
    discovery_method: Literal["source_url", "connector_profile", "search_candidate", "human"]
    confidence: float = Field(ge=0.0, le=1.0)
    source_version_ids: list[str] = Field(default_factory=list)
    evidence_span_ids: list[str] = Field(default_factory=list)
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_observed_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonSearchPlanRevision(StrictModel):
    search_plan_revision_id: str = Field(default_factory=lambda: new_id("splan"))
    person_key: str
    entity_id: str
    revision_number: int = Field(ge=1)
    supersedes_revision_id: str | None = None
    platform_accounts: list[PersonPlatformAccount] = Field(default_factory=list)
    targets: list[PersonSearchTarget] = Field(default_factory=list)
    platform_algorithm_version: str = "platform-tracking-v2"
    timezone: str = "Asia/Shanghai"
    daily_digest_cron: str = "0 18 * * *"
    weekly_digest_cron: str = "30 8 * * 1"
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonProfileImportRequest(StrictModel):
    command_id: str = Field(min_length=1, max_length=200)
    source_version_ids: list[str] = Field(default_factory=list)
    include_all_existing: bool = True
    bootstrap_demo_fixture: bool = False
    created_by: str = "api"


class PersonProfileImportRun(StrictModel):
    import_run_id: str = Field(default_factory=lambda: new_id("pimport"))
    command_id: str
    source_version_ids: list[str] = Field(default_factory=list)
    person_keys: list[str] = Field(default_factory=list)
    created_profile_revision_ids: list[str] = Field(default_factory=list)
    created_search_plan_revision_ids: list[str] = Field(default_factory=list)
    created_source_slice_ids: list[str] = Field(default_factory=list)
    review_batch_ids: list[str] = Field(default_factory=list)
    status: Literal["completed", "partial", "failed"] = "completed"
    counts: dict[str, int] = Field(default_factory=dict)
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonProfileSummary(StrictModel):
    person_key: str
    entity_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    profile_revision_id: str
    search_plan_revision_id: str | None = None
    coverage_percent: float
    source_count: int
    accepted_item_count: int
    pending_item_count: int
    conflicted_item_count: int
    next_scan_at: datetime | None = None
    last_bundle_id: str | None = None
    cognee_namespace: str


class PersonProfileView(StrictModel):
    summary: PersonProfileSummary
    profile: PersonProfileRevision
    search_plan: PersonSearchPlanRevision | None = None
    latest_bundle: "PersonUpdateBundle | None" = None
    latest_review_batch_id: str | None = None


class PersonScanAction(StrictModel):
    sequence: int
    action_type: Literal[
        "read_profile",
        "refresh_known_url",
        "search_channel",
        "version_source",
        "merge_person_delta",
        "extract_field_patch",
        "materialize_profile",
        "project_cognee",
        "emit_digest",
    ]
    title_zh: str
    status: Literal["completed", "queued", "skipped", "failed", "unconfigured"]
    input_zh: str
    result_zh: str
    object_ids: list[str] = Field(default_factory=list)


class PersonProfileScanRequest(StrictModel):
    command_id: str = Field(min_length=1, max_length=200)
    trigger: Literal["manual", "cron", "feishu"] = "manual"
    mode: Literal["replay", "live"] = "replay"
    changed_source_version_ids: list[str] = Field(default_factory=list)
    project_cognee: bool = False


class PersonProfileScanRun(StrictModel):
    person_profile_scan_run_id: str = Field(default_factory=lambda: new_id("pscan"))
    command_id: str
    person_key: str
    entity_id: str
    trigger: Literal["manual", "cron", "feishu"]
    mode: Literal["replay", "live"]
    status: Literal["completed", "partial", "failed", "unchanged"]
    actions: list[PersonScanAction] = Field(default_factory=list)
    bundle_id: str | None = None
    profile_revision_id: str | None = None
    digest_batch_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonUpdateBundle(StrictModel):
    bundle_id: str = Field(default_factory=lambda: new_id("pub"))
    person_key: str
    entity_id: str
    scan_run_id: str
    previous_profile_revision_id: str | None = None
    current_profile_revision_id: str | None = None
    source_slice_ids: list[str] = Field(default_factory=list)
    source_version_ids: list[str] = Field(default_factory=list)
    merged_markdown_ref: str
    merged_markdown_hash: str
    field_deltas: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["new_information", "unchanged", "needs_review"]
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonProfileScanResponse(StrictModel):
    scan_run: PersonProfileScanRun
    bundle: PersonUpdateBundle | None = None
    reused_bundle: bool = False


class PersonDigestBatch(StrictModel):
    digest_batch_id: str = Field(default_factory=lambda: new_id("pdigest"))
    period: Literal["scan", "daily", "weekly"] = "scan"
    bundle_ids: list[str] = Field(default_factory=list)
    person_keys: list[str] = Field(default_factory=list)
    markdown_ref: str
    markdown_hash: str
    created_at: datetime = Field(default_factory=utc_now)


class ProfilePatchReviewItem(StrictModel):
    review_item_id: str = Field(default_factory=lambda: new_id("ppri"))
    review_batch_id: str
    person_key: str
    field_name: str
    field_item_id: str
    target_assertion_id: str | None = None
    title_zh: str
    question_zh: str
    reason_zh: str
    evidence_span_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ProfilePatchReviewBatch(StrictModel):
    review_batch_id: str = Field(default_factory=lambda: new_id("pprb"))
    title_zh: str
    source_ref: str
    person_keys: list[str] = Field(default_factory=list)
    review_item_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ProfilePatchReviewDecision(StrictModel):
    review_decision_id: str = Field(default_factory=lambda: new_id("pprd"))
    command_id: str
    review_item_id: str
    action: Literal["confirm", "reject", "correct", "mark_ambiguous"]
    actor_id: str
    reason: str
    correction_text: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ProfilePatchReviewDecisionRequest(StrictModel):
    command_id: str = Field(min_length=1, max_length=200)
    review_item_id: str
    action: Literal["confirm", "reject", "correct", "mark_ambiguous"]
    actor_id: str
    reason: str = Field(min_length=1, max_length=2000)
    correction_text: str | None = Field(default=None, max_length=5000)


class ProfilePatchReviewBatchView(StrictModel):
    batch: ProfilePatchReviewBatch
    items: list[ProfilePatchReviewItem] = Field(default_factory=list)
    decisions: list[ProfilePatchReviewDecision] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class PersonGraphComparison(StrictModel):
    person_key: str
    profile_revision_id: str
    matched_assertion_ids: list[str] = Field(default_factory=list)
    profile_only_field_item_ids: list[str] = Field(default_factory=list)
    graph_only_assertion_ids: list[str] = Field(default_factory=list)
    conflicted_field_item_ids: list[str] = Field(default_factory=list)
    explanation_zh: list[str] = Field(default_factory=list)


class PersonCogneeProjection(StrictModel):
    projection_id: str = Field(default_factory=lambda: new_id("pcog"))
    person_key: str
    dataset_name: str
    source_slice_ids: list[str] = Field(default_factory=list)
    content_hash: str
    status: Literal["completed", "failed", "unavailable", "unconfigured"]
    duration_ms: float | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonCogneeRecallRequest(StrictModel):
    query_text: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=50)
    query_type: Literal[
        "TEMPORAL",
        "CHUNKS",
        "CHUNKS_LEXICAL",
        "HYBRID_COMPLETION",
        "GRAPH_COMPLETION",
    ] = "CHUNKS"


class PersonCogneeRecallResponse(StrictModel):
    person_key: str
    dataset_name: str
    query_text: str
    query_type: str
    result_count: int
    results: list[Any] = Field(default_factory=list)
    duration_ms: float
    authority: Literal["person_scoped_retrieval_projection_only"] = "person_scoped_retrieval_projection_only"
