from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from people_intel.content_store import ContentAddressedStore
from people_intel.ledger import Ledger
from people_intel.ontology import OntologyRegistry
from people_intel.schemas import (
    Annotation,
    AnnotationAction,
    AnnotationRequest,
    Assertion,
    AssertionRelation,
    AssertionRelationType,
    AssertionStatus,
    CandidatePerson,
    CogneeBatchProjectionItem,
    CogneeBatchProjectionRequest,
    CogneeBatchProjectionResponse,
    CogneeRecallRequest,
    CogneeRecallResponse,
    CogneeRuntimeStatus,
    CognifyRequest,
    CognifyResponse,
    CommandReceipt,
    ConsolidatedReviewDocument,
    ConsolidatedReviewGroup,
    DeriveRequest,
    DerivedAssertionInputs,
    Entity,
    EpistemicType,
    Episode,
    EvidenceSpan,
    ExtractionRun,
    GraphQuery,
    GraphView,
    InitialReviewBatch,
    InitialReviewBatchView,
    InitialReviewDecision,
    InitialReviewDecisionRequest,
    InitialReviewItem,
    InitialReviewItemView,
    ReviewCardAction,
    ReviewDeliveryCard,
    ReviewDeliveryPage,
    ReviewMessageCommandRequest,
    ReviewMessageCommandResult,
    ReviewBulkDecisionRequest,
    ReviewBulkDecisionResponse,
    Polarity,
    Signal,
    SourceDocumentVersion,
    SourceIngestionRequest,
    SourceIngestionResponse,
    WorkingAssertion,
    WorkingView,
    new_id,
    utc_now,
)


class MemoryValidationError(ValueError):
    pass


SOURCE_AUTHORITY = {
    "github": 10.0,
    "arxiv": 10.0,
    "homepage": 9.0,
    "api": 8.0,
    "file": 7.0,
    "feishu": 7.0,
    "news": 5.0,
    "x": 4.0,
    "wechat": 4.0,
    "xhs": 3.0,
    "other": 2.0,
}


class TemporalMemoryService:
    def __init__(
        self,
        ledger: Ledger,
        object_store: ContentAddressedStore,
        ontology: OntologyRegistry,
        *,
        working_view_threshold: float = 0.80,
        working_policy_version: str = "working-view-v1",
        cognify_adapter: Any | None = None,
    ):
        self.ledger = ledger
        self.object_store = object_store
        self.ontology = ontology
        self.working_view_threshold = working_view_threshold
        self.working_policy_version = working_policy_version
        self.cognify_adapter = cognify_adapter

    def ingest_source(self, request: SourceIngestionRequest) -> SourceIngestionResponse:
        raw = self.object_store.put_text(request.content)
        normalized_ref = None
        if request.normalized_markdown is not None:
            normalized_ref = self.object_store.put_text(request.normalized_markdown).object_ref

        versions = sorted(
            self.ledger.find_source_versions(request.source_uri),
            key=lambda item: item.retrieved_at,
        )
        for existing in versions:
            if existing.content_hash == raw.digest:
                return SourceIngestionResponse(
                    source_version_id=existing.source_version_id,
                    content_hash=existing.content_hash,
                    version_status="unchanged",
                    ingestion_job_id=new_id("job"),
                )

        version = SourceDocumentVersion(
            source_uri=request.source_uri,
            source_type=request.source_type,
            media_type=request.media_type,
            content_hash=raw.digest,
            raw_object_ref=raw.object_ref,
            normalized_markdown_ref=normalized_ref,
            published_at=request.published_at,
            retrieved_at=request.retrieved_at,
            source_identity=request.source_identity,
            fetch_method=request.fetch_method,
            rights_scope=request.rights_scope,
            previous_version_id=versions[-1].source_version_id if versions else None,
            metadata=request.metadata,
        )
        self.ledger.append_source_version(version)
        episode = Episode(
            episode_type="source_ingested",
            source_version_ids=[version.source_version_id],
            observed_at=request.retrieved_at,
            context={"source_uri": request.source_uri, "content_hash": raw.digest},
        )
        self.ledger.append_episode(episode)
        return SourceIngestionResponse(
            source_version_id=version.source_version_id,
            content_hash=raw.digest,
            version_status="new_version" if versions else "new_source",
            ingestion_job_id=new_id("job"),
        )

    def get_source_version(self, source_version_id: str) -> SourceDocumentVersion:
        return self.ledger.get_source_version(source_version_id)

    def get_source_text(self, source_version_id: str, *, normalized: bool = True) -> str:
        item = self.ledger.get_source_version(source_version_id)
        ref = item.normalized_markdown_ref if normalized and item.normalized_markdown_ref else item.raw_object_ref
        return self.object_store.read_text(ref)

    def create_span(
        self,
        source_version_id: str,
        *,
        locator_type: str,
        locator: dict[str, Any],
        quote: str,
    ) -> EvidenceSpan:
        source = self.ledger.get_source_version(source_version_id)
        span = EvidenceSpan(
            source_version_id=source_version_id,
            locator_type=locator_type,
            locator=locator,
            quote=quote,
            quote_hash=ContentAddressedStore.digest_text(quote),
            normalized_version_ref=source.normalized_markdown_ref,
        )
        self.ledger.append_evidence_span(span)
        return span

    def create_entity(self, entity: Entity) -> Entity:
        self.ledger.append_entity(entity)
        return entity

    def create_assertion(self, assertion: Assertion) -> Assertion:
        subject = self.ledger.get_entity(assertion.subject_entity_id)
        object_entity = None
        if assertion.object.entity_id:
            object_entity = self.ledger.get_entity(assertion.object.entity_id)
        self.ontology.validate_assertion(assertion, subject, object_entity)
        if assertion.polarity == Polarity.NEGATIVE and not assertion.evidence_span_ids:
            raise MemoryValidationError("negative assertions require explicit evidence")
        for span_id in assertion.evidence_span_ids:
            self.ledger.get_evidence_span(span_id)
        self.ledger.append_assertion(assertion)
        return assertion

    def create_assertion_relation(self, relation: AssertionRelation) -> AssertionRelation:
        self.ledger.append_assertion_relation(relation)
        return relation

    def annotate(self, request: AnnotationRequest) -> Annotation:
        target = self.ledger.get_assertion(request.target_assertion_id)
        replacement_id = None
        if request.action in {AnnotationAction.CORRECT, AnnotationAction.SUPPLEMENT}:
            if request.replacement_assertion is None:
                raise MemoryValidationError(f"{request.action} requires replacement_assertion")
            replacement = request.replacement_assertion.model_copy(
                update={
                    "epistemic_type": EpistemicType.HUMAN_JUDGMENT,
                    "status": AssertionStatus.HUMAN_CONFIRMED,
                    "review_required": False,
                }
            )
            self.create_assertion(replacement)
            replacement_id = replacement.assertion_id
            relation_type = (
                AssertionRelationType.SUPERSEDES
                if request.action == AnnotationAction.CORRECT
                else AssertionRelationType.SUPPORTS
            )
            self.create_assertion_relation(
                AssertionRelation(
                    from_assertion_id=replacement.assertion_id,
                    relation_type=relation_type,
                    to_assertion_id=target.assertion_id,
                    metadata={"annotation_action": request.action},
                )
            )
        annotation = Annotation(
            target_assertion_id=target.assertion_id,
            action=request.action,
            replacement_assertion_id=replacement_id,
            reason=request.reason,
            actor_id=request.actor_id,
            created_at=request.created_at,
        )
        self.ledger.append_annotation(annotation)
        return annotation

    def derive(self, request: DeriveRequest) -> Assertion:
        if not request.input_assertion_ids:
            raise MemoryValidationError("derived assertions require at least one input assertion")
        for assertion_id in request.input_assertion_ids:
            self.ledger.get_assertion(assertion_id)
        derived = request.assertion.model_copy(
            update={
                "epistemic_type": EpistemicType.DERIVED,
                "status": AssertionStatus.MACHINE_PROPOSED,
            }
        )
        self.create_assertion(derived)
        self.ledger.append_derived_inputs(
            DerivedAssertionInputs(
                derived_assertion_id=derived.assertion_id,
                input_assertion_ids=request.input_assertion_ids,
                rule_id=request.rule_id,
                rule_version=request.rule_version,
                model_name=request.model_name,
                prompt_hash=request.prompt_hash,
            )
        )
        return derived

    def cognify(self, source_version_id: str, request: CognifyRequest) -> CognifyResponse:
        source = self.ledger.get_source_version(source_version_id)
        if self.cognify_adapter is None:
            run = ExtractionRun(
                source_version_id=source_version_id,
                extractor=request.extractor,
                extractor_version=request.extractor_version,
                ontology_version=request.ontology_version,
                status="queued",
                metadata={"dataset_name": request.dataset_name, "temporal": request.temporal},
            )
            self.ledger.append_extraction_run(run)
            return CognifyResponse(extraction_run_id=run.extraction_run_id, status="queued")

        for existing in reversed(self.ledger.list_extraction_runs()):
            if (
                existing.source_version_id == source_version_id
                and existing.extractor == request.extractor
                and existing.status == "completed"
                and existing.metadata.get("dataset_name") == request.dataset_name
                and existing.metadata.get("content_hash") == source.content_hash
            ):
                return CognifyResponse(
                    extraction_run_id=existing.extraction_run_id,
                    status="completed",
                    candidate_assertion_count=int(existing.metadata.get("candidate_assertion_count", 0)),
                    metadata={**existing.metadata, "reused_projection": True},
                )

        started_at = utc_now()
        try:
            result = self.cognify_adapter.cognify(
                source=source,
                content=self.get_source_text(source_version_id),
                request=request,
            )
            metadata = {
                "dataset_name": request.dataset_name,
                "temporal": request.temporal,
                "content_hash": source.content_hash,
                "candidate_assertion_count": result.candidate_assertion_count,
                **result.metadata,
            }
            status = result.status
        except Exception as exc:  # Projection failure must not damage source authority.
            metadata = {
                "dataset_name": request.dataset_name,
                "temporal": request.temporal,
                "content_hash": source.content_hash,
                "error_type": type(exc).__name__,
                "error": self._safe_adapter_error(exc),
                "authority": "projection_only",
            }
            status = "failed"
            result = None
        run = ExtractionRun(
            source_version_id=source_version_id,
            extractor=request.extractor,
            extractor_version=request.extractor_version,
            ontology_version=request.ontology_version,
            model_name=self.cognee_status().llm_model,
            started_at=started_at,
            completed_at=utc_now(),
            status=status,
            metadata=metadata,
        )
        self.ledger.append_extraction_run(run)
        return CognifyResponse(
            extraction_run_id=run.extraction_run_id,
            status=status,
            candidate_assertion_count=result.candidate_assertion_count if result else 0,
            metadata=metadata,
        )

    def cognee_status(self) -> CogneeRuntimeStatus:
        if self.cognify_adapter is None:
            return CogneeRuntimeStatus(
                status="unconfigured", configured=False, ready=False,
                dataset_name="people-intel-sandbox", llm_provider="none", llm_model="none",
                credentials_configured=False, embedding_provider="none", embedding_model="none",
                embedding_dimensions=0, relational_db_provider="none", graph_db_provider="none",
                vector_db_provider="none", system_root="", data_root="", vector_db_path="",
                detail_zh="当前服务没有注入 CogneeAdapter。",
            )
        return CogneeRuntimeStatus.model_validate(self.cognify_adapter.runtime_status())

    def project_all_sources(self, request: CogneeBatchProjectionRequest) -> CogneeBatchProjectionResponse:
        runtime = self.cognee_status()
        if not runtime.ready:
            raise MemoryValidationError(runtime.detail_zh)
        requested = set(request.source_version_ids)
        sources = sorted(self.ledger.list_source_versions(), key=lambda item: (item.retrieved_at, item.source_version_id))
        if requested:
            existing = {item.source_version_id for item in sources}
            missing = requested - existing
            if missing:
                raise MemoryValidationError(f"unknown source versions: {sorted(missing)}")
            sources = [item for item in sources if item.source_version_id in requested]

        started_at = utc_now()
        storage_before = runtime.storage
        items: list[CogneeBatchProjectionItem] = []
        for source in sources:
            result = self.cognify(
                source.source_version_id,
                CognifyRequest(
                    extractor="cognee",
                    extractor_version=request.extractor_version,
                    ontology_version=request.ontology_version,
                    dataset_name=request.dataset_name,
                    temporal=request.temporal,
                ),
            )
            items.append(CogneeBatchProjectionItem(
                source_version_id=source.source_version_id,
                content_hash=source.content_hash,
                source_uri=source.source_uri,
                extraction_run_id=result.extraction_run_id,
                status=result.status,
                reused_projection=bool(result.metadata.get("reused_projection", False)),
                projection_document_count=result.metadata.get("projection_document_count"),
                duration_ms=result.metadata.get("duration_ms"),
                error=result.metadata.get("error"),
            ))
            if result.status == "failed" and request.stop_on_failure:
                break

        completed = sum(item.status == "completed" for item in items)
        reused = sum(item.reused_projection for item in items)
        failed = sum(item.status == "failed" for item in items)
        queued = sum(item.status in {"queued", "running"} for item in items)
        total = len(sources)
        return CogneeBatchProjectionResponse(
            dataset_name=request.dataset_name,
            total_sources=total,
            completed=completed,
            reused=reused,
            failed=failed,
            queued=queued,
            coverage_percent=round((completed / total * 100) if total else 100.0, 2),
            started_at=started_at,
            completed_at=utc_now(),
            items=items,
            storage_before=storage_before,
            storage_after=self.cognee_status().storage,
        )

    def recall_cognee(self, request: CogneeRecallRequest) -> CogneeRecallResponse:
        if self.cognify_adapter is None:
            raise MemoryValidationError("当前服务没有配置 CogneeAdapter")
        try:
            value = self.cognify_adapter.recall(
                query_text=request.query_text,
                dataset_name=request.dataset_name,
                top_k=request.top_k,
                query_type=request.query_type,
            )
        except Exception as exc:
            raise MemoryValidationError(self._safe_adapter_error(exc)) from exc
        linked = 0
        enriched_results: list[Any] = []
        sources = self.ledger.list_source_versions()
        for item in value.get("results", []):
            if not isinstance(item, dict):
                enriched_results.append(item)
                continue
            text = item.get("text")
            evidence_candidates: list[dict[str, Any]] = []
            if isinstance(text, str) and text:
                for source in sources:
                    source_text = self.get_source_text(source.source_version_id)
                    start = source_text.find(text)
                    if start >= 0:
                        evidence_candidates.append({
                            "source_version_id": source.source_version_id,
                            "source_uri": source.source_uri,
                            "content_hash": source.content_hash,
                            "char_start": start,
                            "char_end": start + len(text),
                            "match_method": "exact_chunk",
                        })
            if evidence_candidates:
                linked += 1
            enriched_results.append({**item, "evidence_candidates": evidence_candidates})
        value["results"] = enriched_results
        value["evidence_linked_result_count"] = linked
        value["evidence_trace_rate_percent"] = round(
            linked / len(enriched_results) * 100 if enriched_results else 0.0,
            2,
        )
        return CogneeRecallResponse.model_validate(value)

    def create_initial_review_batch(
        self,
        *,
        source_kind: str,
        source_ref: str,
        title_zh: str,
        assertion_ids: list[str],
        source_version_ids: list[str],
        created_by: str,
    ) -> InitialReviewBatchView:
        assertions = [self.ledger.get_assertion(assertion_id) for assertion_id in assertion_ids]
        for existing in self.ledger.list_initial_review_batches():
            if existing.source_kind == source_kind and existing.source_ref == source_ref:
                self._ensure_initial_review_items(existing.review_batch_id, assertions)
                return self.initial_review_batch_view(existing.review_batch_id)
        batch = InitialReviewBatch(
            source_kind=source_kind,
            source_ref=source_ref,
            title_zh=title_zh,
            subject_entity_ids=list(dict.fromkeys(item.subject_entity_id for item in assertions)),
            source_version_ids=list(dict.fromkeys(source_version_ids)),
            created_by=created_by,
            metadata={"review_policy": "initial-profile-human-gate-v1", "item_count": len(assertions)},
        )
        self.ledger.append_initial_review_batch(batch)
        self._ensure_initial_review_items(batch.review_batch_id, assertions)
        return self.initial_review_batch_view(batch.review_batch_id)

    def _ensure_initial_review_items(self, review_batch_id: str, assertions: list[Assertion]) -> None:
        existing_targets = {
            item.target_assertion_id for item in self.ledger.list_initial_review_items(review_batch_id)
        }
        for assertion in assertions:
            if assertion.assertion_id in existing_targets:
                continue
            subject = self.ledger.get_entity(assertion.subject_entity_id)
            object_label = str(assertion.object.value)
            if assertion.object.kind == "entity" and assertion.object.entity_id:
                object_label = self.ledger.get_entity(assertion.object.entity_id).canonical_name
            derived = self.ledger.get_derived_inputs(assertion.assertion_id)
            review_kind = "derived_inference" if derived else "direct_fact"
            if derived:
                rationale = str(assertion.metadata.get("inference_explanation_zh") or (
                    f"这是由 {len(derived.input_assertion_ids)} 条事实通过规则 {derived.rule_id}@{derived.rule_version} 推定的关系；"
                    "需要同时检查输入事实和推理规则。"
                ))
                question = "输入事实成立时，这个逻辑推定是否也应成立？"
                correction_examples = ["确认这条推定", "驳回这条推定：共同出现不代表真实合作", "标记歧义：作者身份尚未消歧"]
            else:
                rationale = "这是首次建档抽取产生的直接候选事实；必须由人确认后才能进入 Working View。"
                question = "原文是否直接支持这条事实？"
                correction_examples = ["确认这条事实", "驳回这条事实：原文不支持", "把对象改成正确的人、论文或账号"]
            self.ledger.append_initial_review_item(
                InitialReviewItem(
                    review_item_id=self._stable_review_item_id(review_batch_id, assertion.assertion_id),
                    review_batch_id=review_batch_id,
                    target_assertion_id=assertion.assertion_id,
                    title_zh=f"{subject.canonical_name} · {assertion.predicate_id} · {object_label}",
                    rationale_zh=rationale,
                    evidence_span_ids=assertion.evidence_span_ids,
                    confidence=assertion.confidence,
                    review_kind=review_kind,
                    question_zh=question,
                    inference_rule_id=derived.rule_id if derived else None,
                    dependency_assertion_ids=derived.input_assertion_ids if derived else [],
                    group_key=str(assertion.metadata.get("group_key") or "") or None,
                    correction_examples_zh=correction_examples,
                )
            )

    @staticmethod
    def _stable_review_item_id(review_batch_id: str, assertion_id: str) -> str:
        digest = hashlib.sha256(f"{review_batch_id}\x1f{assertion_id}".encode("utf-8")).hexdigest()[:32]
        return f"rvi_{digest}"

    def initial_review_batch_view(self, review_batch_id: str) -> InitialReviewBatchView:
        try:
            batch = next(item for item in self.ledger.list_initial_review_batches() if item.review_batch_id == review_batch_id)
        except StopIteration as exc:
            raise MemoryValidationError(f"unknown initial review batch: {review_batch_id}") from exc
        views: list[InitialReviewItemView] = []
        counts = {"pending": 0, "confirmed": 0, "rejected": 0, "corrected": 0, "ambiguous": 0}
        for item in self.ledger.list_initial_review_items(review_batch_id):
            assertion = self.ledger.get_assertion(item.target_assertion_id)
            decisions = sorted(self.ledger.list_initial_review_decisions(item.review_item_id), key=lambda value: value.created_at)
            latest = decisions[-1] if decisions else None
            effective = {
                "confirm": "confirmed", "reject": "rejected", "correct": "corrected", "mark_ambiguous": "ambiguous"
            }.get(latest.action if latest else "", "pending")
            counts[effective] += 1
            object_entity = None
            if assertion.object.kind == "entity" and assertion.object.entity_id:
                object_entity = self.ledger.get_entity(assertion.object.entity_id)
            views.append(InitialReviewItemView(
                item=item,
                assertion=assertion,
                subject=self.ledger.get_entity(assertion.subject_entity_id),
                object_entity=object_entity,
                evidence=[self.ledger.get_evidence_span(span_id) for span_id in item.evidence_span_ids],
                dependency_assertions=[self.ledger.get_assertion(value) for value in item.dependency_assertion_ids],
                latest_decision=latest,
                effective_status=effective,
            ))
        resolved = sum(value for key, value in counts.items() if key != "pending")
        status = "completed" if views and resolved == len(views) else ("in_review" if resolved else "pending")
        return InitialReviewBatchView(batch=batch, status=status, counts=counts, items=views)

    def consolidated_review_document(self, review_batch_id: str) -> ConsolidatedReviewDocument:
        """Return a human-sized review document grouped by the person it describes.

        Account assertions point from an IdentityAccount to a Person.  Grouping
        them by the assertion subject would scatter one person's profile across
        many sections, so the review projection deliberately groups
        ``account_owned_by`` under its object Person.  The underlying Assertion
        remains unchanged.
        """
        view = self.initial_review_batch_view(review_batch_id)
        grouped: dict[str, list[tuple[int, InitialReviewItemView]]] = defaultdict(list)
        for sequence, item in enumerate(view.items, 1):
            subject = self._review_group_subject(item)
            grouped[subject.entity_id].append((sequence, item))
        groups: list[ConsolidatedReviewGroup] = []
        for entries in grouped.values():
            subject = self._review_group_subject(entries[0][1])
            groups.append(ConsolidatedReviewGroup(
                subject=subject,
                sequences=[sequence for sequence, _ in entries],
                pending_count=sum(item.effective_status == "pending" for _, item in entries),
                confirmed_count=sum(item.effective_status == "confirmed" for _, item in entries),
                direct_count=sum(item.item.review_kind == "direct_fact" for _, item in entries),
                derived_count=sum(item.item.review_kind == "derived_inference" for _, item in entries),
                items=[item for _, item in entries],
            ))
        groups.sort(key=lambda group: min(group.sequences) if group.sequences else 0)
        return ConsolidatedReviewDocument(
            review_batch_id=review_batch_id,
            title_zh=view.batch.title_zh,
            status=view.status,
            counts=view.counts,
            groups=groups,
            instructions_zh=[
                "先按人物通读原文摘录和候选事实；账号、获奖与描述性事实集中在同一人物章节。",
                "没有问题时可一次批准全部待审事实，也可只批准当前人物。",
                "需要修改时使用“第N条改为：对象=正确名称；理由=……”；旧 Assertion 不会被覆盖。",
                "逻辑推定单独标识；确认直接事实并不等于自动确认推定关系。",
            ],
            export_markdown_url=f"/v1/initial-reviews/{review_batch_id}/export.md",
        )

    def _review_group_subject(self, item: InitialReviewItemView) -> Entity:
        if (
            item.assertion.predicate_id == "account_owned_by"
            and item.object_entity is not None
            and item.object_entity.entity_type == "Person"
        ):
            return item.object_entity
        return item.subject

    def bulk_decide_initial_review(
        self, review_batch_id: str, request: ReviewBulkDecisionRequest
    ) -> ReviewBulkDecisionResponse:
        existing = self.ledger.get_command_receipt(request.command_id)
        if existing is not None:
            if existing.command_type != "review.bulk_decision" or existing.result.get("review_batch_id") != review_batch_id:
                raise MemoryValidationError("command_id has already been used for another command")
            return ReviewBulkDecisionResponse.model_validate(existing.result["response"])

        view = self.initial_review_batch_view(review_batch_id)
        candidates = [item for item in view.items if item.effective_status == "pending"]
        if request.scope == "subjects":
            requested = set(request.subject_entity_ids)
            if not requested:
                raise MemoryValidationError("subject_entity_ids is required for subjects scope")
            candidates = [item for item in candidates if self._review_group_subject(item).entity_id in requested]
        elif request.scope == "items":
            requested_items = set(request.review_item_ids)
            if not requested_items:
                raise MemoryValidationError("review_item_ids is required for items scope")
            batch_item_ids = {item.item.review_item_id for item in view.items}
            unknown = requested_items - batch_item_ids
            if unknown:
                raise MemoryValidationError(f"review items do not belong to batch: {sorted(unknown)}")
            candidates = [item for item in candidates if item.item.review_item_id in requested_items]

        decisions: list[InitialReviewDecision] = []
        for item in candidates:
            decisions.append(self.decide_initial_review_item(
                item.item.review_item_id,
                InitialReviewDecisionRequest(
                    action=request.action,
                    actor_id=request.actor_id,
                    reason=request.reason,
                ),
            ))
        result = ReviewBulkDecisionResponse(
            command_id=request.command_id,
            action=request.action,
            affected_review_item_ids=[item.item.review_item_id for item in candidates],
            decision_ids=[item.review_decision_id for item in decisions],
            batch=self.initial_review_batch_view(review_batch_id),
        )
        self.ledger.append_command_receipt(CommandReceipt(
            command_id=request.command_id,
            command_type="review.bulk_decision",
            status="completed",
            result={"review_batch_id": review_batch_id, "response": result.model_dump(mode="json")},
        ))
        return result

    def initial_review_delivery(
        self,
        review_batch_id: str,
        *,
        channel: str = "feishu",
        cursor: int = 0,
        page_size: int = 5,
        pending_only: bool = False,
    ) -> ReviewDeliveryPage:
        view = self.initial_review_batch_view(review_batch_id)
        indexed = list(enumerate(view.items, 1))
        if pending_only:
            indexed = [value for value in indexed if value[1].effective_status == "pending"]
        selected = indexed[cursor:cursor + page_size]
        cards: list[ReviewDeliveryCard] = []
        for sequence, entry in selected:
            assertion = entry.assertion
            object_label = str(assertion.object.value)
            if entry.object_entity:
                object_label = entry.object_entity.canonical_name
            fact = f"{entry.subject.canonical_name} — {assertion.predicate_id} → {object_label}"
            chain = [self._assertion_sentence(item) for item in entry.dependency_assertions]
            if entry.item.inference_rule_id:
                chain.append(f"规则：{entry.item.inference_rule_id} → {fact}")
            actions = [
                ReviewCardAction(
                    action=action,
                    label_zh=label,
                    command_text=(f"确认第{sequence}条" if action == "confirm" else f"{label}第{sequence}条：请补充原因"),
                    postback={
                        "endpoint": f"/v1/initial-review-items/{entry.item.review_item_id}/decisions",
                        "body": {"action": action, "actor_id": "{{feishu_open_id}}", "reason": "{{optional_reason}}"},
                    },
                )
                for action, label in (("confirm", "确认"), ("reject", "驳回"), ("mark_ambiguous", "标记歧义"))
            ]
            cards.append(ReviewDeliveryCard(
                sequence=sequence,
                review_item_id=entry.item.review_item_id,
                review_kind=entry.item.review_kind,
                status=entry.effective_status,
                title_zh=entry.item.title_zh,
                fact_zh=fact,
                question_zh=entry.item.question_zh,
                why_zh=entry.item.rationale_zh,
                evidence_quotes=[item.quote for item in entry.evidence],
                inference_chain_zh=chain,
                confidence=entry.item.confidence,
                actions=actions,
                correction_examples_zh=[
                    *entry.item.correction_examples_zh,
                    f"第{sequence}条改为：对象=正确名称；理由=原对象识别有误",
                ],
            ))
        next_cursor = cursor + len(selected) if cursor + len(selected) < len(indexed) else None
        return ReviewDeliveryPage(
            review_batch_id=review_batch_id,
            channel=channel,
            cursor=cursor,
            next_cursor=next_cursor,
            page_size=page_size,
            total=len(indexed),
            pending_total=view.counts.get("pending", 0),
            summary_zh=(
                f"本批次共 {len(view.items)} 条：直接事实 {sum(item.item.review_kind == 'direct_fact' for item in view.items)} 条，"
                f"逻辑推定 {sum(item.item.review_kind == 'derived_inference' for item in view.items)} 条，"
                f"仍待确认 {view.counts.get('pending', 0)} 条。"
            ),
            cards=cards,
            message_examples_zh=[
                "确认第1条",
                "确认第1,2,3条",
                "驳回第4条：不是同一个人",
                "第5条标记歧义：作者身份尚未消歧",
                "第6条改为：对象=正确名称；理由=原对象识别有误",
                "确认全部",
            ],
        )

    def apply_initial_review_message(
        self, review_batch_id: str, request: ReviewMessageCommandRequest
    ) -> ReviewMessageCommandResult:
        existing = self.ledger.get_command_receipt(request.command_id)
        if existing is not None:
            if existing.command_type != "review.message" or existing.result.get("review_batch_id") != review_batch_id:
                raise MemoryValidationError("command_id has already been used for another command")
            return ReviewMessageCommandResult.model_validate(existing.result["response"])

        view = self.initial_review_batch_view(review_batch_id)
        text = request.text.strip()
        action: str | None = None
        indexes: list[int] = []
        reason = ""
        correction_spec: str | None = None
        if re.fullmatch(r"确认全部[。！!]?", text):
            action = "confirm"
            indexes = [index for index, item in enumerate(view.items, 1) if item.effective_status == "pending"]
            reason = "用户通过消息确认全部待处理事实。"
        else:
            multi = re.fullmatch(r"确认(?:第)?\s*([0-9,，、\s]+)\s*条?[。！!]?", text)
            reject = re.fullmatch(r"驳回(?:第)?\s*(\d+)\s*条?\s*[：:]\s*(.+)", text)
            ambiguous = re.fullmatch(r"第?\s*(\d+)\s*条?\s*标记歧义\s*[：:]\s*(.+)", text)
            correct = re.fullmatch(r"第?\s*(\d+)\s*条?\s*改为\s*[：:]\s*(.+)", text)
            if multi:
                action = "confirm"
                indexes = [int(value) for value in re.findall(r"\d+", multi.group(1))]
                reason = "用户通过消息确认指定事实。"
            elif reject:
                action, indexes, reason = "reject", [int(reject.group(1))], reject.group(2).strip()
            elif ambiguous:
                action, indexes, reason = "mark_ambiguous", [int(ambiguous.group(1))], ambiguous.group(2).strip()
            elif correct:
                action, indexes, correction_spec = "correct", [int(correct.group(1))], correct.group(2).strip()

        matched = bool(action and indexes)
        decisions: list[InitialReviewDecision] = []
        affected: list[str] = []
        feedback = "未识别这条指令。请使用页面给出的示例格式，系统不会猜测你的修改意图。"
        if matched:
            if any(value < 1 or value > len(view.items) for value in indexes):
                raise MemoryValidationError(f"review item sequence must be between 1 and {len(view.items)}")
            for index in list(dict.fromkeys(indexes)):
                entry = view.items[index - 1]
                if action == "correct":
                    replacement, correction_reason = self._replacement_from_message(entry, correction_spec or "")
                    decision_request = InitialReviewDecisionRequest(
                        action="correct", reason=correction_reason, actor_id=request.actor_id,
                        replacement_assertion=replacement,
                    )
                else:
                    decision_request = InitialReviewDecisionRequest(
                        action=action, reason=reason, actor_id=request.actor_id,
                    )
                decision = self.decide_initial_review_item(entry.item.review_item_id, decision_request)
                decisions.append(decision)
                affected.append(entry.item.review_item_id)
            feedback = f"已处理 {len(decisions)} 条：{action}。原 Assertion 与证据仍保留在审计时间线中。"

        refreshed = self.initial_review_batch_view(review_batch_id)
        result = ReviewMessageCommandResult(
            command_id=request.command_id,
            matched=matched,
            action=action,
            affected_review_item_ids=affected,
            decision_ids=[item.review_decision_id for item in decisions],
            feedback_zh=feedback,
            batch=refreshed,
        )
        self.ledger.append_command_receipt(CommandReceipt(
            command_id=request.command_id,
            command_type="review.message",
            status="completed" if matched else "rejected",
            result={"review_batch_id": review_batch_id, "response": result.model_dump(mode="json")},
        ))
        return result

    def _replacement_from_message(
        self, entry: InitialReviewItemView, specification: str
    ) -> tuple[Assertion, str]:
        values: dict[str, str] = {}
        for part in re.split(r"[；;]", specification):
            if "=" in part:
                key, value = part.split("=", 1)
                values[key.strip()] = value.strip()
        object_name = values.get("对象")
        if not object_name:
            raise MemoryValidationError("修正指令必须包含“对象=正确名称”")
        predicate_id = values.get("谓词", entry.assertion.predicate_id)
        reason = values.get("理由", "用户通过消息修正候选事实。")
        subject = entry.subject
        object_entity = None
        pending_entity = None
        if entry.assertion.object.kind == "entity":
            if entry.object_entity is None:
                raise MemoryValidationError("原 Assertion 缺少 object entity")
            object_entity = next(
                (
                    item for item in self.ledger.list_entities()
                    if item.entity_type == entry.object_entity.entity_type
                    and (item.canonical_name == object_name or object_name in item.aliases)
                ),
                None,
            )
            if object_entity is None:
                pending_entity = Entity(entity_type=entry.object_entity.entity_type, canonical_name=object_name)
                object_entity = pending_entity
            object_value = entry.assertion.object.model_copy(update={"entity_id": object_entity.entity_id})
        else:
            object_value = entry.assertion.object.model_copy(update={"value": object_name})
        replacement = entry.assertion.model_copy(update={
            "assertion_id": new_id("ast"),
            "predicate_id": predicate_id,
            "object": object_value,
            "transaction_time": entry.assertion.transaction_time.model_copy(update={"ingested_at": utc_now()}),
            "metadata": {**entry.assertion.metadata, "corrected_via": "review_message"},
        })
        self.ontology.validate_assertion(replacement, subject, object_entity)
        if pending_entity is not None:
            self.create_entity(pending_entity)
        return replacement, reason

    def _assertion_sentence(self, assertion: Assertion) -> str:
        subject = self.ledger.get_entity(assertion.subject_entity_id).canonical_name
        if assertion.object.entity_id:
            object_label = self.ledger.get_entity(assertion.object.entity_id).canonical_name
        else:
            object_label = str(assertion.object.value)
        return f"{subject} — {assertion.predicate_id} → {object_label}"

    def decide_initial_review_item(
        self, review_item_id: str, request: InitialReviewDecisionRequest
    ) -> InitialReviewDecision:
        try:
            item = next(item for item in self.ledger.list_initial_review_items() if item.review_item_id == review_item_id)
        except StopIteration as exc:
            raise MemoryValidationError(f"unknown initial review item: {review_item_id}") from exc
        for existing in reversed(self.ledger.list_initial_review_decisions(review_item_id)):
            if existing.action == request.action and existing.reason == request.reason and existing.actor_id == request.actor_id:
                return existing
        annotation = self.annotate(AnnotationRequest(
            target_assertion_id=item.target_assertion_id,
            action=AnnotationAction(request.action),
            replacement_assertion=request.replacement_assertion,
            reason=request.reason,
            actor_id=request.actor_id,
        ))
        decision = InitialReviewDecision(
            review_item_id=review_item_id,
            action=request.action,
            reason=request.reason,
            actor_id=request.actor_id,
            annotation_id=annotation.annotation_id,
            replacement_assertion_id=annotation.replacement_assertion_id,
        )
        self.ledger.append_initial_review_decision(decision)
        return decision

    def initial_review_markdown(self, review_batch_id: str) -> str:
        view = self.initial_review_batch_view(review_batch_id)
        lines = [
            f"# {view.batch.title_zh}", "", f"- batch: `{view.batch.review_batch_id}`",
            f"- status: `{view.status}`", f"- policy: `initial-profile-human-gate-v1`", "",
            "> 首次建档候选事实必须由人确认；逻辑推定还必须检查输入事实与规则。未处理项目不会进入 Working View。", "",
            "## 可直接回复的指令", "",
            "- `确认第1条` 或 `确认第1,2,3条`",
            "- `驳回第4条：说明原因`",
            "- `第5条标记歧义：说明尚不确定的边界`",
            "- `第6条改为：对象=正确名称；理由=原对象识别有误`",
            "- `确认全部`", "",
        ]
        for index, item in enumerate(view.items, 1):
            lines.extend([
                f"## {index}. {item.item.title_zh}", "",
                f"- 类型：`{item.item.review_kind}`",
                f"- 裁决：**{item.effective_status}**",
                f"- Assertion：`{item.assertion.assertion_id}`",
                f"- 置信度：`{item.item.confidence:.2f}`",
                f"- valid_time：`{item.assertion.valid_time.model_dump(mode='json')}`",
                f"- 证据：{'；'.join(span.quote for span in item.evidence) or '无'}",
                f"- 要确认的问题：{item.item.question_zh}",
                f"- 说明：{item.item.rationale_zh}", "",
            ])
            if item.item.review_kind == "derived_inference":
                lines.extend([
                    f"- 推理规则：`{item.item.inference_rule_id}`",
                    "- 输入事实：",
                    *[f"  - `{dependency.assertion_id}`：{self._assertion_sentence(dependency)}" for dependency in item.dependency_assertions],
                    "",
                ])
            if item.latest_decision:
                lines.extend([
                    f"- 人工反馈：{item.latest_decision.reason}",
                    f"- 裁决人：`{item.latest_decision.actor_id}`",
                    f"- Annotation：`{item.latest_decision.annotation_id}`", "",
                ])
        return "\n".join(lines)

    def _safe_adapter_error(self, exc: Exception) -> str:
        message = str(exc)
        config = getattr(self.cognify_adapter, "config", None)
        secret = getattr(config, "llm_api_key", "") if config else ""
        if secret:
            message = message.replace(secret, "[REDACTED]")
        return message[:2000]

    def effective_status(self, assertion: Assertion, known_at: datetime) -> AssertionStatus:
        status = AssertionStatus(assertion.status)
        annotations = sorted(
            [item for item in self.ledger.list_annotations(assertion.assertion_id) if item.created_at <= known_at],
            key=lambda item: item.created_at,
        )
        for annotation in annotations:
            action = AnnotationAction(annotation.action)
            if action == AnnotationAction.CONFIRM:
                status = AssertionStatus.HUMAN_CONFIRMED
            elif action == AnnotationAction.REJECT:
                status = AssertionStatus.HUMAN_REJECTED
            elif action == AnnotationAction.CORRECT:
                status = AssertionStatus.RETRACTED
            elif action == AnnotationAction.MARK_AMBIGUOUS:
                status = AssertionStatus.CONFLICTED

        derived = self.ledger.get_derived_inputs(assertion.assertion_id)
        if derived:
            for dependency_id in derived.input_assertion_ids:
                dependency = self.ledger.get_assertion(dependency_id)
                dependency_status = self.effective_status(dependency, known_at)
                if dependency_status in {AssertionStatus.HUMAN_REJECTED, AssertionStatus.RETRACTED}:
                    return AssertionStatus.RETRACTED
        return status

    def timeline(
        self,
        entity_id: str,
        *,
        known_at: datetime | None = None,
        include_derived: bool = True,
    ) -> list[dict[str, Any]]:
        self.ledger.get_entity(entity_id)
        cutoff = known_at or utc_now()
        result = []
        for assertion in self.ledger.list_assertions(entity_id):
            if not assertion.is_known_at(cutoff):
                continue
            if not include_derived and assertion.epistemic_type == EpistemicType.DERIVED:
                continue
            result.append(
                {
                    "assertion": assertion,
                    "effective_status": self.effective_status(assertion, cutoff),
                    "annotations": [
                        item
                        for item in self.ledger.list_annotations(assertion.assertion_id)
                        if item.created_at <= cutoff
                    ],
                    "evidence": [self.ledger.get_evidence_span(key) for key in assertion.evidence_span_ids],
                }
            )
        return sorted(result, key=lambda item: item["assertion"].transaction_time.ingested_at)

    def working_view(self, query: GraphQuery) -> WorkingView:
        self.ledger.get_entity(query.subject_entity_id)
        candidates: list[tuple[Assertion, AssertionStatus, float, list[str]]] = []
        excluded: list[str] = []
        initial_review_targets = {item.target_assertion_id for item in self.ledger.list_initial_review_items()}
        for assertion in self.ledger.list_assertions(query.subject_entity_id):
            if query.predicates and assertion.predicate_id not in query.predicates:
                continue
            if not assertion.is_known_at(query.known_at) or not assertion.applies_at(query.valid_at):
                excluded.append(assertion.assertion_id)
                continue
            if not query.include_derived and assertion.epistemic_type == EpistemicType.DERIVED:
                excluded.append(assertion.assertion_id)
                continue
            status = self.effective_status(assertion, query.known_at)
            if status in {AssertionStatus.HUMAN_REJECTED, AssertionStatus.RETRACTED}:
                excluded.append(assertion.assertion_id)
                continue
            derived_inputs = self.ledger.get_derived_inputs(assertion.assertion_id)
            if derived_inputs and any(
                self.effective_status(self.ledger.get_assertion(dependency_id), query.known_at)
                != AssertionStatus.HUMAN_CONFIRMED
                for dependency_id in derived_inputs.input_assertion_ids
            ):
                # Confirming a logical conclusion is not a substitute for
                # confirming its premises. It becomes eligible automatically
                # once every dependency is human-confirmed.
                excluded.append(assertion.assertion_id)
                continue
            if (assertion.review_required or assertion.assertion_id in initial_review_targets) and status != AssertionStatus.HUMAN_CONFIRMED:
                excluded.append(assertion.assertion_id)
                continue
            if status != AssertionStatus.HUMAN_CONFIRMED and assertion.confidence < self.working_view_threshold:
                excluded.append(assertion.assertion_id)
                continue
            score, reasons = self._working_score(assertion, status)
            candidates.append((assertion, status, score, reasons))

        by_predicate: dict[str, list[tuple[Assertion, AssertionStatus, float, list[str]]]] = defaultdict(list)
        for candidate in candidates:
            by_predicate[candidate[0].predicate_id].append(candidate)

        selected: list[WorkingAssertion] = []
        for predicate_id, group in by_predicate.items():
            predicate = self.ontology.get(predicate_id)
            if not predicate.multi_valued:
                ordered = sorted(group, key=lambda item: (-item[2], item[0].assertion_id))
                winner = ordered[0]
                selected.append(self._to_working(winner, [item[0].assertion_id for item in ordered[1:]], query.valid_at))
                continue

            by_object: dict[str, list[tuple[Assertion, AssertionStatus, float, list[str]]]] = defaultdict(list)
            for candidate in group:
                by_object[self._object_key(candidate[0])].append(candidate)
            for object_group in by_object.values():
                ordered = sorted(object_group, key=lambda item: (-item[2], item[0].assertion_id))
                winner = ordered[0]
                selected.append(self._to_working(winner, [item[0].assertion_id for item in ordered[1:]], query.valid_at))

        selected.sort(key=lambda item: (item.assertion.predicate_id, -item.score, item.assertion.assertion_id))
        return WorkingView(
            entity_id=query.subject_entity_id,
            valid_at=query.valid_at,
            known_at=query.known_at,
            policy_version=self.working_policy_version,
            selected=selected,
            excluded_assertion_ids=sorted(set(excluded)),
        )

    def graph_query(self, query: GraphQuery) -> WorkingView | list[dict[str, Any]]:
        if query.view == GraphView.WORKING:
            return self.working_view(query)
        if query.view == GraphView.EVIDENCE_TIMELINE:
            timeline = self.timeline(
                query.subject_entity_id,
                known_at=query.known_at,
                include_derived=query.include_derived,
            )
            if query.predicates:
                timeline = [
                    item for item in timeline if item["assertion"].predicate_id in query.predicates
                ]
            if not query.include_evidence:
                for item in timeline:
                    item["evidence"] = []
            return timeline
        return self._assertion_graph(query)

    def _assertion_graph(self, query: GraphQuery) -> list[dict[str, Any]]:
        """Breadth-first, bounded graph expansion over authoritative assertions.

        Symmetric predicates are traversable from either endpoint while the
        immutable Assertion keeps its original canonical storage direction.
        """
        self.ledger.get_entity(query.subject_entity_id)
        queue: deque[tuple[str, int, list[str]]] = deque(
            [(query.subject_entity_id, 0, [query.subject_entity_id])]
        )
        expanded: set[str] = set()
        emitted: set[str] = set()
        result: list[dict[str, Any]] = []
        while queue:
            entity_id, depth, path = queue.popleft()
            if entity_id in expanded or depth >= query.max_depth:
                continue
            expanded.add(entity_id)
            incident: list[tuple[Assertion, str, str | None]] = [
                (assertion, "outgoing", assertion.object.entity_id)
                for assertion in self.ledger.list_assertions(entity_id)
            ]
            for assertion in self.ledger.list_assertions():
                object_id = assertion.object.entity_id
                if object_id != entity_id or assertion.subject_entity_id == entity_id:
                    continue
                if not self.ontology.get(assertion.predicate_id).symmetric:
                    continue
                incident.append((assertion, "incoming_symmetric", assertion.subject_entity_id))
            for assertion, direction, neighbour_id in incident:
                if query.predicates and assertion.predicate_id not in query.predicates:
                    continue
                if not assertion.is_known_at(query.known_at):
                    continue
                if not query.include_derived and assertion.epistemic_type == EpistemicType.DERIVED:
                    continue
                if assertion.assertion_id in emitted:
                    continue
                emitted.add(assertion.assertion_id)
                assertion_path = [*path, assertion.assertion_id]
                if neighbour_id:
                    assertion_path.append(neighbour_id)
                result.append(
                    {
                        "depth": depth + 1,
                        "path": assertion_path,
                        "direction": direction,
                        "assertion": assertion,
                        "effective_status": self.effective_status(assertion, query.known_at),
                        "evidence": (
                            [self.ledger.get_evidence_span(key) for key in assertion.evidence_span_ids]
                            if query.include_evidence
                            else []
                        ),
                    }
                )
                if neighbour_id and neighbour_id not in expanded:
                    queue.append((neighbour_id, depth + 1, assertion_path))
        return result

    def identity_cluster(self, entity_id: str, *, known_at: datetime | None = None) -> list[str]:
        self.ledger.get_entity(entity_id)
        cutoff = known_at or utc_now()
        allowed = {"same_as", "possibly_same_as", "alias_of", "account_owned_by"}
        blocked: set[frozenset[str]] = set()
        adjacency: dict[str, set[str]] = defaultdict(set)
        for assertion in self.ledger.list_assertions():
            if assertion.object.entity_id is None or not assertion.is_known_at(cutoff):
                continue
            if self.effective_status(assertion, cutoff) in {AssertionStatus.HUMAN_REJECTED, AssertionStatus.RETRACTED}:
                continue
            pair = frozenset({assertion.subject_entity_id, assertion.object.entity_id})
            if assertion.predicate_id == "not_same_as":
                blocked.add(pair)
            elif assertion.predicate_id in allowed:
                adjacency[assertion.subject_entity_id].add(assertion.object.entity_id)
                adjacency[assertion.object.entity_id].add(assertion.subject_entity_id)

        visited = {entity_id}
        queue = deque([entity_id])
        while queue:
            current = queue.popleft()
            for neighbour in adjacency[current]:
                if frozenset({current, neighbour}) in blocked or neighbour in visited:
                    continue
                visited.add(neighbour)
                queue.append(neighbour)
        return sorted(visited)

    def create_signal_for_assertion(self, assertion_id: str) -> Signal | None:
        from people_intel.signals import GraphSignalEngine

        emitted = GraphSignalEngine(self.ledger, self.ontology).evaluate_assertion(assertion_id)
        return emitted[0] if emitted else None

    def add_candidate(self, candidate: CandidatePerson) -> CandidatePerson:
        self.ledger.append_candidate(candidate)
        return candidate

    def _working_score(self, assertion: Assertion, status: AssertionStatus) -> tuple[float, list[str]]:
        score = assertion.confidence * 100.0
        reasons = [f"confidence={assertion.confidence:.2f}"]
        if status == AssertionStatus.HUMAN_CONFIRMED:
            score += 100.0
            reasons.append("human_confirmed")
        if assertion.epistemic_type == EpistemicType.DERIVED:
            score -= 15.0
            reasons.append("derived_penalty")
        for span_id in assertion.evidence_span_ids:
            span = self.ledger.get_evidence_span(span_id)
            source = self.ledger.get_source_version(span.source_version_id)
            authority = SOURCE_AUTHORITY.get(str(source.source_type), 0.0)
            score += authority
            reasons.append(f"source:{source.source_type}+{authority:.0f}")
        if assertion.valid_time.start and assertion.valid_time.start.precision not in {"unknown", "range"}:
            score += 3.0
            reasons.append("specific_valid_time")
        return score, reasons

    @staticmethod
    def _object_key(assertion: Assertion) -> str:
        if assertion.object.entity_id:
            return f"entity:{assertion.object.entity_id}:{assertion.polarity}"
        return f"literal:{assertion.object.datatype}:{assertion.object.value!r}:{assertion.polarity}"

    @staticmethod
    def _to_working(
        candidate: tuple[Assertion, AssertionStatus, float, list[str]],
        alternatives: list[str],
        valid_at: datetime,
    ) -> WorkingAssertion:
        assertion, status, score, reasons = candidate
        return WorkingAssertion(
            assertion=assertion,
            effective_status=status,
            score=score,
            temporal_certainty=assertion.valid_time.certainty_at(valid_at),
            alternative_assertion_ids=alternatives,
            selection_reasons=reasons,
        )
