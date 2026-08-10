from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_assignment=True,
        protected_namespaces=(),
    )


class SourceType(StrEnum):
    GITHUB = "github"
    ARXIV = "arxiv"
    HOMEPAGE = "homepage"
    X = "x"
    WECHAT = "wechat"
    XHS = "xhs"
    NEWS = "news"
    FEISHU = "feishu"
    FILE = "file"
    API = "api"
    OTHER = "other"


class FetchMethod(StrEnum):
    API = "api"
    QIAOMU = "qiaomu"
    AGENT_REACH = "agent_reach"
    UPLOAD = "upload"
    FEISHU = "feishu"
    MANUAL = "manual"


class RightsScope(StrEnum):
    PUBLIC = "public"
    USER_SUPPLIED = "user_supplied"
    RESTRICTED = "restricted"


class EntityType(StrEnum):
    PERSON = "Person"
    IDENTITY_ACCOUNT = "IdentityAccount"
    ORGANIZATION = "Organization"
    INSTITUTION = "Institution"
    LAB_OR_TEAM = "LabOrTeam"
    COMPANY = "Company"
    PROJECT = "Project"
    REPOSITORY = "Repository"
    PAPER = "Paper"
    PRODUCT = "Product"
    FUNDING_ROUND = "FundingRound"
    ROLE = "Role"
    EVENT = "Event"
    AWARD = "Award"
    TOPIC = "Topic"
    LOCATION = "Location"


class TemporalPrecision(StrEnum):
    SECOND = "second"
    MINUTE = "minute"
    DAY = "day"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    RANGE = "range"
    UNKNOWN = "unknown"


class IntervalType(StrEnum):
    CLOSED = "closed"
    OPEN_START = "open_start"
    OPEN_END = "open_end"
    OPEN = "open"
    INSTANT = "instant"
    UNKNOWN = "unknown"


class ObjectKind(StrEnum):
    ENTITY = "entity"
    LITERAL = "literal"


class LiteralDatatype(StrEnum):
    STRING = "string"
    NUMBER = "number"
    MONEY = "money"
    DATE = "date"
    URL = "url"
    BOOLEAN = "boolean"
    JSON = "json"


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNCERTAIN = "uncertain"


class EpistemicType(StrEnum):
    OBSERVED = "observed"
    REPORTED = "reported"
    HUMAN_JUDGMENT = "human_judgment"
    DERIVED = "derived"


class AssertionStatus(StrEnum):
    MACHINE_PROPOSED = "machine_proposed"
    HUMAN_CONFIRMED = "human_confirmed"
    HUMAN_REJECTED = "human_rejected"
    RETRACTED = "retracted"
    CONFLICTED = "conflicted"


class AnnotationAction(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    CORRECT = "correct"
    SUPPLEMENT = "supplement"
    MARK_AMBIGUOUS = "mark_ambiguous"
    SPLIT_IDENTITY = "split_identity"
    LINK_IDENTITY = "link_identity"


class AssertionRelationType(StrEnum):
    CONTRADICTS = "contradicts"
    SUPPORTS = "supports"
    SUPERSEDES = "supersedes"
    REFINES = "refines"
    RETRACTS = "retracts"


class GraphView(StrEnum):
    ALL_ASSERTIONS = "all_assertions"
    EVIDENCE_TIMELINE = "evidence_timeline"
    WORKING = "working"


class SignalType(StrEnum):
    PUBLICATION = "publication"
    REPOSITORY_CREATED = "repository_created"
    RELEASE = "release"
    HOMEPAGE_CHANGE = "homepage_change"
    ROLE_CHANGE = "role_change"
    AFFILIATION_CHANGE = "affiliation_change"
    STARTUP_LAUNCH = "startup_launch"
    FUNDING = "funding"
    PRODUCT_LAUNCH = "product_launch"
    AWARD = "award"
    TEAM_FORMATION = "team_formation"
    DATA_QUALITY_CONFLICT = "data_quality_conflict"
    OTHER = "other"


class TemporalPoint(StrictModel):
    earliest: datetime | None = None
    latest: datetime | None = None
    precision: TemporalPrecision = TemporalPrecision.UNKNOWN

    @model_validator(mode="after")
    def validate_bounds(self) -> "TemporalPoint":
        if self.earliest and self.latest and self.earliest > self.latest:
            raise ValueError("earliest must not be after latest")
        return self


class TemporalExtent(StrictModel):
    start: TemporalPoint | None = None
    end: TemporalPoint | None = None
    interval_type: IntervalType = IntervalType.UNKNOWN
    timezone: str = "UTC"

    @model_validator(mode="after")
    def validate_interval(self) -> "TemporalExtent":
        if self.interval_type == IntervalType.INSTANT and self.start is None:
            raise ValueError("instant intervals require start")
        start_floor = self.start.earliest if self.start else None
        end_ceiling = self.end.latest if self.end else None
        if start_floor and end_ceiling and start_floor > end_ceiling:
            raise ValueError("valid time start must not be after end")
        return self

    def contains(self, instant: datetime) -> bool:
        if self.start and self.start.earliest and instant < self.start.earliest:
            return False
        if self.end and self.end.latest and instant > self.end.latest:
            return False
        return True

    def certainty_at(self, instant: datetime) -> Literal["certain", "uncertain", "outside"]:
        if not self.contains(instant):
            return "outside"
        if self.start and self.start.latest and instant < self.start.latest:
            return "uncertain"
        if self.end and self.end.earliest and instant > self.end.earliest:
            return "uncertain"
        return "certain"


class TransactionTime(StrictModel):
    observed_at: datetime
    ingested_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_order(self) -> "TransactionTime":
        if self.ingested_at < self.observed_at:
            raise ValueError("ingested_at must not precede observed_at")
        return self


class SourceDocumentVersion(StrictModel):
    source_version_id: str = Field(default_factory=lambda: new_id("srcv"))
    source_uri: str
    source_type: SourceType
    media_type: str
    content_hash: str
    raw_object_ref: str
    normalized_markdown_ref: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    source_identity: str | None = None
    fetch_method: FetchMethod
    rights_scope: RightsScope = RightsScope.PUBLIC
    previous_version_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Episode(StrictModel):
    episode_id: str = Field(default_factory=lambda: new_id("ep"))
    episode_type: str
    source_version_ids: list[str] = Field(default_factory=list)
    entity_candidate_ids: list[str] = Field(default_factory=list)
    scan_run_id: str | None = None
    occurred_at: TemporalExtent | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    actor_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class EvidenceSpan(StrictModel):
    evidence_span_id: str = Field(default_factory=lambda: new_id("span"))
    source_version_id: str
    locator_type: Literal["page", "paragraph", "dom", "json_pointer", "character", "record", "whole_document"]
    locator: dict[str, Any] = Field(default_factory=dict)
    quote: str
    quote_hash: str
    normalized_version_ref: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Entity(StrictModel):
    entity_id: str = Field(default_factory=lambda: new_id("ent"))
    entity_type: EntityType
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class PredicateDefinition(StrictModel):
    predicate_id: str
    domain: list[str]
    range: list[str]
    temporal_kind: Literal["event", "state"]
    multi_valued: bool = True
    allow_negative: bool = False
    conflict_strategy: Literal["rank", "coexist"] = "coexist"
    symmetric: bool = False


class OntologyDefinition(StrictModel):
    version: str
    predicates: list[PredicateDefinition]


class ExtractionRun(StrictModel):
    extraction_run_id: str = Field(default_factory=lambda: new_id("xrun"))
    source_version_id: str
    extractor: str
    extractor_version: str
    ontology_version: str
    prompt_hash: str | None = None
    model_name: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssertionObject(StrictModel):
    kind: ObjectKind
    entity_id: str | None = None
    datatype: LiteralDatatype | None = None
    value: Any = None

    @model_validator(mode="after")
    def validate_shape(self) -> "AssertionObject":
        if self.kind == ObjectKind.ENTITY:
            if not self.entity_id or self.datatype is not None or self.value is not None:
                raise ValueError("entity objects require entity_id and no literal value")
        else:
            if self.entity_id is not None or self.datatype is None:
                raise ValueError("literal objects require datatype and no entity_id")
        return self


class Assertion(StrictModel):
    assertion_id: str = Field(default_factory=lambda: new_id("ast"))
    subject_entity_id: str
    predicate_id: str
    object: AssertionObject
    polarity: Polarity = Polarity.POSITIVE
    epistemic_type: EpistemicType = EpistemicType.REPORTED
    status: AssertionStatus = AssertionStatus.MACHINE_PROPOSED
    valid_time: TemporalExtent = Field(default_factory=TemporalExtent)
    transaction_time: TransactionTime
    evidence_span_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    extraction_run_id: str | None = None
    ontology_version: str = "people-intel-v1"
    review_required: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_known_at(self, known_at: datetime) -> bool:
        return self.transaction_time.ingested_at <= known_at

    def applies_at(self, valid_at: datetime) -> bool:
        return self.valid_time.contains(valid_at)


class AssertionRelation(StrictModel):
    assertion_relation_id: str = Field(default_factory=lambda: new_id("arel"))
    from_assertion_id: str
    relation_type: AssertionRelationType
    to_assertion_id: str
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Annotation(StrictModel):
    annotation_id: str = Field(default_factory=lambda: new_id("ann"))
    target_assertion_id: str
    action: AnnotationAction
    replacement_assertion_id: str | None = None
    reason: str
    actor_id: str
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InitialReviewBatch(StrictModel):
    review_batch_id: str = Field(default_factory=lambda: new_id("rvb"))
    source_kind: Literal["story_session", "file_import", "feishu_import", "api_import"]
    source_ref: str
    title_zh: str
    subject_entity_ids: list[str] = Field(default_factory=list)
    source_version_ids: list[str] = Field(default_factory=list)
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InitialReviewItem(StrictModel):
    review_item_id: str = Field(default_factory=lambda: new_id("rvi"))
    review_batch_id: str
    target_assertion_id: str
    title_zh: str
    rationale_zh: str
    evidence_span_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    review_kind: Literal["direct_fact", "derived_inference"] = "direct_fact"
    question_zh: str = "这条事实是否准确？"
    inference_rule_id: str | None = None
    dependency_assertion_ids: list[str] = Field(default_factory=list)
    group_key: str | None = None
    correction_examples_zh: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class InitialReviewDecision(StrictModel):
    review_decision_id: str = Field(default_factory=lambda: new_id("rvd"))
    review_item_id: str
    action: Literal["confirm", "reject", "correct", "mark_ambiguous"]
    reason: str
    actor_id: str
    annotation_id: str
    replacement_assertion_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class InitialReviewDecisionRequest(StrictModel):
    action: Literal["confirm", "reject", "correct", "mark_ambiguous"]
    reason: str = Field(min_length=1, max_length=2000)
    actor_id: str = Field(min_length=1, max_length=200)
    replacement_assertion: Assertion | None = None


class InitialReviewItemView(StrictModel):
    item: InitialReviewItem
    assertion: Assertion
    subject: Entity
    object_entity: Entity | None = None
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    dependency_assertions: list[Assertion] = Field(default_factory=list)
    latest_decision: InitialReviewDecision | None = None
    effective_status: Literal["pending", "confirmed", "rejected", "corrected", "ambiguous"]


class InitialReviewBatchView(StrictModel):
    batch: InitialReviewBatch
    status: Literal["pending", "in_review", "completed"]
    counts: dict[str, int] = Field(default_factory=dict)
    items: list[InitialReviewItemView] = Field(default_factory=list)


class ReviewCardAction(StrictModel):
    action: Literal["confirm", "reject", "mark_ambiguous", "correct"]
    label_zh: str
    command_text: str
    postback: dict[str, Any]


class ReviewDeliveryCard(StrictModel):
    sequence: int
    review_item_id: str
    review_kind: Literal["direct_fact", "derived_inference"]
    status: Literal["pending", "confirmed", "rejected", "corrected", "ambiguous"]
    title_zh: str
    fact_zh: str
    question_zh: str
    why_zh: str
    evidence_quotes: list[str] = Field(default_factory=list)
    inference_chain_zh: list[str] = Field(default_factory=list)
    confidence: float
    actions: list[ReviewCardAction] = Field(default_factory=list)
    correction_examples_zh: list[str] = Field(default_factory=list)


class ReviewDeliveryPage(StrictModel):
    review_batch_id: str
    channel: Literal["web", "feishu", "markdown", "api"]
    cursor: int
    next_cursor: int | None = None
    page_size: int
    total: int
    pending_total: int
    summary_zh: str
    cards: list[ReviewDeliveryCard] = Field(default_factory=list)
    message_examples_zh: list[str] = Field(default_factory=list)


class ReviewMessageCommandRequest(StrictModel):
    command_id: str = Field(default_factory=lambda: new_id("cmd"))
    text: str = Field(min_length=1, max_length=4000)
    actor_id: str = Field(min_length=1, max_length=200)
    channel: Literal["web", "feishu", "api"] = "feishu"


class ReviewMessageCommandResult(StrictModel):
    command_id: str
    matched: bool
    action: str | None = None
    affected_review_item_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
    feedback_zh: str
    batch: InitialReviewBatchView


class InitialKnowledgeImportRequest(StrictModel):
    source_uri: str
    content: str = Field(min_length=1)
    normalized_markdown: str | None = None
    media_type: str = "text/markdown"
    observed_at: datetime = Field(default_factory=utc_now)
    created_by: str = Field(default="demo-user", min_length=1, max_length=200)
    cohort_id: str = "icml-2026-award-contributors"


class InitialKnowledgeImportResponse(StrictModel):
    source_version_id: str
    review_batch_id: str
    version_status: Literal["new_source", "new_version", "unchanged"]
    counts: dict[str, int] = Field(default_factory=dict)
    person_entity_ids: list[str] = Field(default_factory=list)
    paper_entity_ids: list[str] = Field(default_factory=list)
    assertion_ids: list[str] = Field(default_factory=list)
    derived_assertion_ids: list[str] = Field(default_factory=list)
    evidence_span_ids: list[str] = Field(default_factory=list)


class ConsolidatedReviewGroup(StrictModel):
    subject: Entity
    sequences: list[int] = Field(default_factory=list)
    pending_count: int = 0
    confirmed_count: int = 0
    direct_count: int = 0
    derived_count: int = 0
    items: list[InitialReviewItemView] = Field(default_factory=list)


class ConsolidatedReviewDocument(StrictModel):
    review_batch_id: str
    title_zh: str
    status: Literal["pending", "in_review", "completed"]
    counts: dict[str, int] = Field(default_factory=dict)
    groups: list[ConsolidatedReviewGroup] = Field(default_factory=list)
    instructions_zh: list[str] = Field(default_factory=list)
    export_markdown_url: str


class ReviewBulkDecisionRequest(StrictModel):
    command_id: str = Field(default_factory=lambda: new_id("cmd"))
    actor_id: str = Field(min_length=1, max_length=200)
    action: Literal["confirm", "reject", "mark_ambiguous"]
    scope: Literal["all_pending", "subjects", "items"] = "all_pending"
    subject_entity_ids: list[str] = Field(default_factory=list)
    review_item_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=2000)


class ReviewBulkDecisionResponse(StrictModel):
    command_id: str
    action: Literal["confirm", "reject", "mark_ambiguous"]
    affected_review_item_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
    batch: InitialReviewBatchView


class DirectoryWatchScanRequest(StrictModel):
    directory_path: str
    importer: Literal["apple_scholars", "none"] = "apple_scholars"
    recursive: bool = True
    created_by: str = Field(default="demo-user", min_length=1, max_length=200)
    observed_at: datetime = Field(default_factory=utc_now)


class TrackedFileResult(StrictModel):
    relative_path: str
    media_type: str
    size_bytes: int
    content_hash: str
    object_ref: str
    status: Literal["new", "changed", "unchanged"]


class DirectoryWatchScanResponse(StrictModel):
    directory_uri: str
    manifest_source_version_id: str
    version_status: Literal["new_source", "new_version", "unchanged"]
    files: list[TrackedFileResult] = Field(default_factory=list)
    removed_paths: list[str] = Field(default_factory=list)
    review_batch_ids: list[str] = Field(default_factory=list)
    imported_people: int = 0


class FeishuDocumentScanRequest(StrictModel):
    source_uri: str
    content: str = Field(min_length=1)
    normalized_markdown: str | None = None
    importer: Literal["apple_scholars", "none"] = "apple_scholars"
    created_by: str = Field(default="feishu-user", min_length=1, max_length=200)
    observed_at: datetime = Field(default_factory=utc_now)


class FeishuDocumentScanResponse(StrictModel):
    source_version_id: str
    version_status: Literal["new_source", "new_version", "unchanged"]
    review_batch_id: str | None = None
    imported_people: int = 0


class DerivedAssertionInputs(StrictModel):
    derived_assertion_id: str
    input_assertion_ids: list[str]
    rule_id: str
    rule_version: str
    model_name: str | None = None
    prompt_hash: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Signal(StrictModel):
    signal_id: str = Field(default_factory=lambda: new_id("sig"))
    signal_type: SignalType
    title: str
    summary: str
    score: float = Field(ge=0.0, le=100.0)
    confidence: float = Field(ge=0.0, le=1.0)
    entity_ids: list[str]
    triggering_assertion_ids: list[str]
    evidence_span_ids: list[str]
    alternative_assertion_ids: list[str] = Field(default_factory=list)
    time_boundary: TemporalExtent | None = None
    graph_path: list[str] = Field(default_factory=list)
    rule_id: str
    rule_version: str
    dedupe_key: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class CandidatePerson(StrictModel):
    candidate_id: str = Field(default_factory=lambda: new_id("cand"))
    entity_id: str
    discovered_from_entity_ids: list[str] = Field(default_factory=list)
    evidence_span_ids: list[str] = Field(default_factory=list)
    reason: str
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    status: Literal["candidate", "tracking", "dismissed"] = "candidate"
    created_at: datetime = Field(default_factory=utc_now)


class WorkingAssertion(StrictModel):
    assertion: Assertion
    effective_status: AssertionStatus
    score: float
    temporal_certainty: Literal["certain", "uncertain", "outside"]
    alternative_assertion_ids: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)


class WorkingView(StrictModel):
    entity_id: str
    valid_at: datetime
    known_at: datetime
    policy_version: str
    selected: list[WorkingAssertion]
    excluded_assertion_ids: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)


class GraphSubject(StrictModel):
    entity_id: str


class GraphQuery(StrictModel):
    subject: GraphSubject
    predicates: list[str] = Field(default_factory=list)
    valid_at: datetime = Field(default_factory=utc_now)
    known_at: datetime = Field(default_factory=utc_now)
    view: GraphView = GraphView.WORKING
    include_evidence: bool = True
    include_derived: bool = True
    max_depth: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="before")
    @classmethod
    def accept_flat_subject(cls, value: Any) -> Any:
        if isinstance(value, dict) and "subject" not in value and "subject_entity_id" in value:
            value = dict(value)
            value["subject"] = {"entity_id": value.pop("subject_entity_id")}
        return value

    @property
    def subject_entity_id(self) -> str:
        return self.subject.entity_id


class SourceIngestionRequest(StrictModel):
    source_uri: str
    source_type: SourceType
    media_type: str = "text/plain"
    content: str
    normalized_markdown: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    source_identity: str | None = None
    fetch_method: FetchMethod = FetchMethod.UPLOAD
    rights_scope: RightsScope = RightsScope.PUBLIC
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceIngestionResponse(StrictModel):
    source_version_id: str
    content_hash: str
    version_status: Literal["new_source", "new_version", "unchanged"]
    ingestion_job_id: str


class AnnotationRequest(StrictModel):
    target_assertion_id: str
    action: AnnotationAction
    replacement_assertion: Assertion | None = None
    reason: str
    actor_id: str
    created_at: datetime = Field(default_factory=utc_now)


class DeriveRequest(StrictModel):
    assertion: Assertion
    input_assertion_ids: list[str]
    rule_id: str
    rule_version: str
    model_name: str | None = None
    prompt_hash: str | None = None


class CognifyRequest(StrictModel):
    extractor: str = "cognee"
    extractor_version: str = "1"
    ontology_version: str = "people-intel-v1"
    dataset_name: str = "people-intel-sandbox"
    temporal: bool = True


class CognifyResponse(StrictModel):
    extraction_run_id: str
    status: Literal["queued", "running", "completed", "failed"]
    candidate_assertion_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class CogneeRuntimeStatus(StrictModel):
    status: Literal["ready", "degraded", "unavailable", "unconfigured"]
    configured: bool
    ready: bool
    package_version: str | None = None
    dataset_name: str
    llm_provider: str
    llm_model: str
    llm_endpoint: str | None = None
    credentials_configured: bool
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    relational_db_provider: str
    graph_db_provider: str
    vector_db_provider: str
    system_root: str
    data_root: str
    vector_db_path: str
    storage: dict[str, Any] = Field(default_factory=dict)
    detail_zh: str
    authority: Literal["projection_only"] = "projection_only"


class CogneeRecallRequest(StrictModel):
    query_text: str = Field(min_length=1, max_length=2000)
    dataset_name: str | None = None
    top_k: int = Field(default=10, ge=1, le=50)
    query_type: Literal[
        "TEMPORAL",
        "CHUNKS",
        "CHUNKS_LEXICAL",
        "HYBRID_COMPLETION",
        "GRAPH_COMPLETION",
    ] = "TEMPORAL"


class CogneeRecallResponse(StrictModel):
    query_text: str
    dataset_name: str
    top_k: int
    query_type: str = "TEMPORAL"
    result_count: int
    results: list[Any] = Field(default_factory=list)
    evidence_linked_result_count: int = 0
    evidence_trace_rate_percent: float = 0.0
    duration_ms: float
    authority: Literal["retrieval_projection_only"] = "retrieval_projection_only"


class CogneeBatchProjectionRequest(StrictModel):
    source_version_ids: list[str] = Field(default_factory=list)
    extractor_version: str = "1.4"
    ontology_version: str = "people-intel-v1"
    dataset_name: str = "people-intel-sandbox"
    temporal: bool = True
    stop_on_failure: bool = False


class CogneeBatchProjectionItem(StrictModel):
    source_version_id: str
    content_hash: str
    source_uri: str
    extraction_run_id: str
    status: Literal["queued", "running", "completed", "failed"]
    reused_projection: bool = False
    projection_document_count: int | None = None
    duration_ms: float | None = None
    error: str | None = None


class CogneeBatchProjectionResponse(StrictModel):
    dataset_name: str
    total_sources: int
    completed: int
    reused: int
    failed: int
    queued: int
    coverage_percent: float
    started_at: datetime
    completed_at: datetime
    items: list[CogneeBatchProjectionItem]
    storage_before: dict[str, Any] = Field(default_factory=dict)
    storage_after: dict[str, Any] = Field(default_factory=dict)
    authority: Literal["projection_only"] = "projection_only"


class CommandActor(StrictModel):
    channel: Literal["feishu_dm", "feishu_group", "base", "file", "api"]
    actor_id: str
    chat_id: str | None = None


class CommandEnvelope(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    command_id: str = Field(default_factory=lambda: new_id("cmd"))
    command_type: Literal[
        "source.ingest",
        "assertion.annotate",
        "identity.link",
        "identity.split",
        "scan.request",
        "candidate.promote",
    ]
    occurred_at: datetime = Field(default_factory=utc_now)
    actor: CommandActor
    payload: dict[str, Any]


class CommandReceipt(StrictModel):
    command_id: str
    command_type: str
    status: Literal["completed", "accepted", "rejected"]
    result: dict[str, Any] = Field(default_factory=dict)
    processed_at: datetime = Field(default_factory=utc_now)


class ComponentStatus(StrictModel):
    component_id: str
    name: str
    layer: Literal["authority", "projection", "connector", "interface", "runtime"]
    status: Literal["ready", "degraded", "unconfigured", "unavailable"]
    authoritative: bool = False
    detail: str


class CapabilityDefinition(StrictModel):
    capability_id: str
    name: str
    status: Literal["implemented", "degraded", "unconfigured"]
    endpoint_paths: list[str] = Field(default_factory=list)
    demo_step_ids: list[str] = Field(default_factory=list)
    test_refs: list[str] = Field(default_factory=list)


class SystemManifest(StrictModel):
    app_version: str
    environment: Literal["sandbox", "live-readonly", "live-write"]
    ontology_version: str
    schema_hash: str
    snapshot_root_hash: str | None = None
    counts: dict[str, int]
    metrics: dict[str, int | float | str | None]
    capabilities: list[CapabilityDefinition]
    components: list[ComponentStatus]
    live_available: bool = False
    live_writes_allowed: bool = False
    demo_session_id: str | None = None
    verification: dict[str, Any] | None = None
    generated_at: datetime = Field(default_factory=utc_now)


TPage = TypeVar("TPage")


class CollectionPage(StrictModel, Generic[TPage]):
    items: list[TPage]
    total: int
    offset: int
    limit: int
    next_offset: int | None = None


class DomainEvent(StrictModel):
    event_id: int
    event_type: str
    aggregate_type: str
    aggregate_id: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    trace_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class DemoSession(StrictModel):
    session_id: str
    scenario_id: str = "complete-mvp"
    status: Literal["ready", "running", "completed", "failed"] = "ready"
    current_step: int = 0
    total_steps: int = 10
    focus_entity_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DemoScenarioStep(StrictModel):
    step_id: str
    position: int
    title: str
    description: str
    endpoint: str


class DemoScenario(StrictModel):
    scenario_id: str
    name: str
    description: str
    steps: list[DemoScenarioStep]


class DemoRunRequest(StrictModel):
    step: int | None = Field(default=None, ge=1, le=10)


class ScenarioStepResult(StrictModel):
    session_id: str
    scenario_id: str
    step_id: str
    position: int
    title: str
    status: Literal["completed", "failed", "already_completed"]
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    changed_ids: list[str] = Field(default_factory=list)
    event_id: int | None = None
    completed_at: datetime = Field(default_factory=utc_now)


class GraphNode(StrictModel):
    id: str
    node_type: Literal["entity", "assertion", "evidence", "source", "literal"]
    label: str
    subtype: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(StrictModel):
    id: str
    source: str
    target: str
    label: str


class MemoryGraph(StrictModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class MemoryLayerDefinition(StrictModel):
    layer_id: str
    title_zh: str
    role_zh: str
    stores_zh: list[str] = Field(default_factory=list)
    authoritative: bool
    rebuildable: bool
    rebuild_source_zh: str
    runtime_component_id: str | None = None
    endpoint_paths: list[str] = Field(default_factory=list)


class CogneeArchitectureDefinition(StrictModel):
    integration_mode_zh: str
    ingestion_call: str
    retrieval_call: str
    dataset_strategy_zh: str
    graph_role_zh: str
    embedding_role_zh: str
    temporal_role_zh: str
    output_rule_zh: str
    authority_zh: str
    self_improvement: bool
    rebuild_policy_zh: str
    current_limitations_zh: list[str] = Field(default_factory=list)


class MemoryWriteStepDefinition(StrictModel):
    step_id: str
    position: int
    title_zh: str
    input_zh: str
    action_zh: str
    output_zh: str
    authority_effect_zh: str
    cognee_role_zh: str
    endpoint_path: str | None = None
    event_type: str | None = None


class GraphLegendDefinition(StrictModel):
    node_type: Literal["entity", "assertion", "evidence", "source", "literal"]
    title_zh: str
    role_zh: str


class MemoryArchitectureDefinition(StrictModel):
    architecture_version: str
    title_zh: str
    knowledge_unit_zh: str
    authority_rule_zh: str
    layers: list[MemoryLayerDefinition]
    cognee: CogneeArchitectureDefinition
    write_path: list[MemoryWriteStepDefinition]
    graph_legend: list[GraphLegendDefinition]


class KnowledgeJourneyStep(StrictModel):
    step_id: str
    position: int
    title_zh: str
    status: Literal["completed", "failed", "skipped", "available_not_invoked", "unconfigured", "pending"]
    actual_result_zh: str
    input_ids: list[str] = Field(default_factory=list)
    output_ids: list[str] = Field(default_factory=list)
    graph_node_ids: list[str] = Field(default_factory=list)


class KnowledgeJourney(StrictModel):
    session_id: str
    story_id: str
    scan_run_id: str
    subject_name: str
    assertion_created: bool
    stop_reason_zh: str | None = None
    steps: list[KnowledgeJourneyStep]
    graph_delta: MemoryGraph


class ScanRun(StrictModel):
    scan_run_id: str = Field(default_factory=lambda: new_id("scan"))
    story_id: str
    subject_name: str
    trigger: str
    mode: Literal["replay", "live"] = "replay"
    channels: list[str] = Field(default_factory=list)
    status: Literal["running", "completed", "partial", "failed"] = "running"
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorAttempt(StrictModel):
    connector_attempt_id: str = Field(default_factory=lambda: new_id("cat"))
    scan_run_id: str
    channel: str
    route: str
    request_summary: str
    status: Literal["planned", "running", "completed", "failed", "unconfigured"]
    duration_ms: int | None = None
    raw_object_ref: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorRunRequest(StrictModel):
    command_id: str = Field(min_length=1, max_length=200)
    channel: Literal["homepage", "github", "arxiv", "x", "wechat", "xhs", "news"]
    query: str = Field(min_length=1, max_length=500)
    source_uri: str | None = None
    subject_name: str = Field(default="manual-source-scan", min_length=1, max_length=300)
    trigger: Literal["manual", "cron", "feishu"] = "manual"
    max_results: int = Field(default=5, ge=1, le=20)
    persist_full_results: bool = True
    tracking_key: str | None = Field(default=None, min_length=1, max_length=200)
    person_key: str | None = Field(default=None, min_length=1, max_length=200)
    query_kind: str | None = Field(default=None, min_length=1, max_length=100)
    platform_account_key: str | None = Field(default=None, min_length=1, max_length=200)


class ConnectorRunResponse(StrictModel):
    command_id: str
    scan_run: ScanRun
    attempt: ConnectorAttempt
    candidates: list["DiscoveryCandidate"] = Field(default_factory=list)
    source_version_ids: list[str] = Field(default_factory=list)
    normalized_preview: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorJobEnqueueRequest(StrictModel):
    request: ConnectorRunRequest
    max_attempts: int = Field(default=3, ge=1, le=8)
    not_before: datetime = Field(default_factory=utc_now)


class ConnectorJob(StrictModel):
    connector_job_id: str = Field(default_factory=lambda: new_id("cjob"))
    command_id: str
    request: ConnectorRunRequest
    max_attempts: int = Field(default=3, ge=1, le=8)
    not_before: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


class ConnectorJobEvent(StrictModel):
    connector_job_event_id: str = Field(default_factory=lambda: new_id("cje"))
    connector_job_id: str
    event_type: Literal["queued", "claimed", "deferred", "retry_scheduled", "succeeded", "dead_lettered"]
    worker_id: str | None = None
    attempt: int = Field(default=0, ge=0)
    lease_expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)


class ConnectorJobView(StrictModel):
    job: ConnectorJob
    status: Literal["queued", "running", "retry_wait", "succeeded", "dead_lettered"]
    attempt_count: int = 0
    last_event: ConnectorJobEvent | None = None
    events: list[ConnectorJobEvent] = Field(default_factory=list)


class ConnectorPolicyDefinition(StrictModel):
    channel: Literal["homepage", "github", "arxiv", "x", "wechat", "xhs", "news"]
    min_interval_seconds: int = Field(ge=0)
    daily_attempt_limit: int = Field(ge=1)
    circuit_failure_threshold: int = Field(ge=1)
    circuit_cooldown_seconds: int = Field(ge=1)
    serialized: bool = False
    rationale_zh: str


class ConnectorPolicyDecision(StrictModel):
    allowed: bool
    reason: Literal["ready", "minimum_interval", "daily_limit", "circuit_open"]
    next_attempt_at: datetime | None = None
    detail_zh: str


class ConnectorScheduleSubscription(StrictModel):
    subscription_id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:-]+$")
    subject_entity_id: str | None = None
    subject_name: str = Field(min_length=1, max_length=300)
    channel: Literal["homepage", "github", "arxiv", "x", "wechat", "xhs", "news"]
    query: str = Field(min_length=1, max_length=500)
    source_uri: str | None = None
    interval_minutes: int = Field(default=1440, ge=5, le=10080)
    max_results: int = Field(default=5, ge=1, le=20)
    max_attempts: int = Field(default=3, ge=1, le=8)
    enabled: bool = True
    person_key: str | None = Field(default=None, min_length=1, max_length=200)
    query_kind: str | None = Field(default=None, min_length=1, max_length=100)
    platform_account_key: str | None = Field(default=None, min_length=1, max_length=200)


class ConnectorScheduleConfig(StrictModel):
    timezone: str = "Asia/Shanghai"
    subscriptions: list[ConnectorScheduleSubscription] = Field(default_factory=list)


class ConnectorScheduleRunResult(StrictModel):
    evaluated_at: datetime
    subscription_count: int
    due_count: int
    enqueued_count: int
    existing_count: int
    disabled_count: int
    connector_job_ids: list[str] = Field(default_factory=list)


class DiscoveryCandidate(StrictModel):
    discovery_candidate_id: str = Field(default_factory=lambda: new_id("disc"))
    scan_run_id: str
    channel: str
    rank: int = Field(ge=1)
    title: str
    uri: str
    published_at: datetime | None = None
    decision: Literal["selected", "skipped", "pending"] = "pending"
    reason_zh: str
    created_at: datetime = Field(default_factory=utc_now)


class ChangeBlock(StrictModel):
    kind: Literal["added", "removed", "changed"]
    before: str | None = None
    after: str | None = None
    label_zh: str


class ChangeSet(StrictModel):
    change_set_id: str = Field(default_factory=lambda: new_id("chg"))
    scan_run_id: str
    source_uri: str
    previous_source_version_id: str | None = None
    current_source_version_id: str
    version_status: Literal["new_source", "new_version", "unchanged"]
    blocks: list[ChangeBlock] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowArtifact(StrictModel):
    artifact_id: str = Field(default_factory=lambda: new_id("art"))
    scan_run_id: str
    stage_id: str
    kind: Literal[
        "trigger", "source_plan", "search_results", "source_preview", "version_diff",
        "evidence", "identity_decision", "graph_delta", "working_view", "signal_card",
        "delivery_preview", "connector_status",
    ]
    title_zh: str
    summary_zh: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_version_ids: list[str] = Field(default_factory=list)
    evidence_span_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    assertion_ids: list[str] = Field(default_factory=list)
    signal_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowStageDefinition(StrictModel):
    stage_id: str
    position: int
    title_zh: str
    purpose_zh: str
    decision_zh: str
    endpoint_paths: list[str] = Field(default_factory=list)
    schema_refs: list[str] = Field(default_factory=list)
    predicate_ids: list[str] = Field(default_factory=list)
    test_refs: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    function_zh: str = ""
    trigger_zh: str = ""
    input_zh: list[str] = Field(default_factory=list)
    actions_zh: list[str] = Field(default_factory=list)
    local_result_zh: list[str] = Field(default_factory=list)
    aggregation_zh: str = ""
    exception_zh: str = ""
    sample_story_id: str | None = None
    technical_reference_ids: list[str] = Field(default_factory=list)


class PredicatePresentation(StrictModel):
    predicate_id: str
    label_zh: str
    description_zh: str
    example_zh: str


class ProductFunctionDefinition(StrictModel):
    function_id: str
    title_zh: str
    user_goal_zh: str
    input_zh: str
    action_zh: str
    output_zh: str


class ScheduleDefinition(StrictModel):
    schedule_id: str
    title_zh: str
    cron: str
    timezone: str = "Asia/Shanghai"
    target_zh: str
    actions_zh: list[str]
    output_zh: str


class SourceChannelDefinition(StrictModel):
    channel: str
    title_zh: str
    discovery_method_zh: str
    normalization_method_zh: str
    use_for_zh: str
    selection_rule_zh: str


class CohortExampleDefinition(StrictModel):
    person_name: str
    tier: Literal["A", "B", "C"]
    reason_zh: str
    channels: list[str]
    scan_frequency_zh: str
    next_action_zh: str


class ReferenceFieldDefinition(StrictModel):
    name: str
    meaning_zh: str


class TechnicalReferenceDefinition(StrictModel):
    reference_id: str
    kind: Literal["api", "schema", "event", "algorithm", "test", "ui"]
    title_zh: str
    summary_zh: str
    used_when_zh: str
    behavior_zh: list[str] = Field(default_factory=list)
    fields: list[ReferenceFieldDefinition] = Field(default_factory=list)
    related_reference_ids: list[str] = Field(default_factory=list)
    source_ref: str | None = None
    example_input: dict[str, Any] | None = None
    example_output: dict[str, Any] | None = None


class WorkflowDefinition(StrictModel):
    workflow_version: str
    title_zh: str
    product_purpose_zh: str = ""
    product_functions: list[ProductFunctionDefinition] = Field(default_factory=list)
    cohort_examples: list[CohortExampleDefinition] = Field(default_factory=list)
    schedules: list[ScheduleDefinition] = Field(default_factory=list)
    source_channels: list[SourceChannelDefinition] = Field(default_factory=list)
    stages: list[WorkflowStageDefinition]
    predicate_presentations: list[PredicatePresentation]
    technical_references: list[TechnicalReferenceDefinition] = Field(default_factory=list)


class DemoStory(StrictModel):
    story_id: str
    title_zh: str
    subject_name: str
    category_zh: str
    promise_zh: str
    outcome_zh: str
    channels: list[str]
    default: bool = False
    fixture_hash: str


class StorySessionCreate(StrictModel):
    story_id: str
    mode: Literal["replay", "live"] = "replay"


class StorySession(StrictModel):
    session_id: str = Field(default_factory=lambda: new_id("story"))
    story_id: str
    mode: Literal["replay", "live"] = "replay"
    scan_run_id: str
    status: Literal["ready", "running", "completed", "partial", "failed"] = "ready"
    current_stage: int = 0
    total_stages: int = 8
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class StoryStepResult(StrictModel):
    session_id: str
    story_id: str
    stage_id: str
    position: int
    status: Literal["completed", "partial", "failed", "already_completed"]
    narration_zh: str
    decision_zh: str
    artifact_ids: list[str] = Field(default_factory=list)
    domain_event_ids: list[int] = Field(default_factory=list)
    changed_ids: list[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=utc_now)


class RecordingManifest(StrictModel):
    recording_id: str
    story_id: str
    app_version: str
    schema_hash: str
    fixture_hash: str
    video_path: str
    poster_path: str
    recorded_at: datetime
