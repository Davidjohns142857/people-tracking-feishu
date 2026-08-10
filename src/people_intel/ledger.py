from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Lock
from typing import Protocol

from people_intel.schemas import (
    Annotation,
    Assertion,
    AssertionRelation,
    CandidatePerson,
    CommandReceipt,
    DerivedAssertionInputs,
    Entity,
    Episode,
    EvidenceSpan,
    ExtractionRun,
    InitialReviewBatch,
    InitialReviewDecision,
    InitialReviewItem,
    Signal,
    ScanRun,
    ConnectorAttempt,
    ConnectorJob,
    ConnectorJobEvent,
    DiscoveryCandidate,
    ChangeSet,
    WorkflowArtifact,
    SourceDocumentVersion,
)
from people_intel.person_profile_schemas import (
    PersonCogneeProjection,
    PersonDigestBatch,
    PersonKeyRecord,
    PersonProfileImportRun,
    PersonProfileRevision,
    PersonProfileScanRun,
    PersonSearchPlanRevision,
    PersonSourceSlice,
    PersonUpdateBundle,
    ProfilePatchReviewBatch,
    ProfilePatchReviewDecision,
    ProfilePatchReviewItem,
)


class LedgerConflict(ValueError):
    pass


class LedgerNotFound(KeyError):
    pass


class Ledger(Protocol):
    def append_source_version(self, item: SourceDocumentVersion) -> None: ...
    def get_source_version(self, source_version_id: str) -> SourceDocumentVersion: ...
    def find_source_versions(self, source_uri: str) -> list[SourceDocumentVersion]: ...
    def list_source_versions(self) -> list[SourceDocumentVersion]: ...
    def append_episode(self, item: Episode) -> None: ...
    def list_episodes(self) -> list[Episode]: ...
    def append_evidence_span(self, item: EvidenceSpan) -> None: ...
    def get_evidence_span(self, evidence_span_id: str) -> EvidenceSpan: ...
    def list_evidence_spans(self, source_version_id: str | None = None) -> list[EvidenceSpan]: ...
    def append_entity(self, item: Entity) -> None: ...
    def get_entity(self, entity_id: str) -> Entity: ...
    def list_entities(self) -> list[Entity]: ...
    def append_extraction_run(self, item: ExtractionRun) -> None: ...
    def list_extraction_runs(self) -> list[ExtractionRun]: ...
    def append_initial_review_batch(self, item: InitialReviewBatch) -> None: ...
    def list_initial_review_batches(self) -> list[InitialReviewBatch]: ...
    def append_initial_review_item(self, item: InitialReviewItem) -> None: ...
    def list_initial_review_items(self, review_batch_id: str | None = None) -> list[InitialReviewItem]: ...
    def append_initial_review_decision(self, item: InitialReviewDecision) -> None: ...
    def list_initial_review_decisions(self, review_item_id: str | None = None) -> list[InitialReviewDecision]: ...
    def append_assertion(self, item: Assertion) -> None: ...
    def get_assertion(self, assertion_id: str) -> Assertion: ...
    def list_assertions(self, subject_entity_id: str | None = None) -> list[Assertion]: ...
    def append_assertion_relation(self, item: AssertionRelation) -> None: ...
    def list_assertion_relations(self) -> list[AssertionRelation]: ...
    def append_annotation(self, item: Annotation) -> None: ...
    def list_annotations(self, target_assertion_id: str | None = None) -> list[Annotation]: ...
    def append_derived_inputs(self, item: DerivedAssertionInputs) -> None: ...
    def get_derived_inputs(self, derived_assertion_id: str) -> DerivedAssertionInputs | None: ...
    def list_derived_inputs(self) -> list[DerivedAssertionInputs]: ...
    def append_signal(self, item: Signal) -> None: ...
    def list_signals(self) -> list[Signal]: ...
    def append_candidate(self, item: CandidatePerson) -> None: ...
    def list_candidates(self) -> list[CandidatePerson]: ...
    def append_command_receipt(self, item: CommandReceipt) -> None: ...
    def get_command_receipt(self, command_id: str) -> CommandReceipt | None: ...
    def append_scan_run(self, item: ScanRun) -> None: ...
    def list_scan_runs(self) -> list[ScanRun]: ...
    def append_connector_attempt(self, item: ConnectorAttempt) -> None: ...
    def list_connector_attempts(self, scan_run_id: str | None = None) -> list[ConnectorAttempt]: ...
    def append_discovery_candidate(self, item: DiscoveryCandidate) -> None: ...
    def list_discovery_candidates(self, scan_run_id: str | None = None) -> list[DiscoveryCandidate]: ...
    def append_change_set(self, item: ChangeSet) -> None: ...
    def list_change_sets(self, scan_run_id: str | None = None) -> list[ChangeSet]: ...
    def append_workflow_artifact(self, item: WorkflowArtifact) -> None: ...
    def list_workflow_artifacts(self, scan_run_id: str | None = None) -> list[WorkflowArtifact]: ...
    def append_connector_job(self, item: ConnectorJob) -> None: ...
    def get_connector_job(self, connector_job_id: str) -> ConnectorJob: ...
    def find_connector_job_by_command_id(self, command_id: str) -> ConnectorJob | None: ...
    def list_connector_jobs(self) -> list[ConnectorJob]: ...
    def append_connector_job_event(self, item: ConnectorJobEvent) -> None: ...
    def list_connector_job_events(self, connector_job_id: str | None = None) -> list[ConnectorJobEvent]: ...
    def claim_next_connector_job(self, worker_id: str, lease_seconds: int, now: datetime) -> tuple[ConnectorJob, ConnectorJobEvent] | None: ...
    def append_person_key(self, item: PersonKeyRecord) -> None: ...
    def list_person_keys(self) -> list[PersonKeyRecord]: ...
    def append_person_source_slice(self, item: PersonSourceSlice) -> None: ...
    def list_person_source_slices(self, person_key: str | None = None) -> list[PersonSourceSlice]: ...
    def append_person_profile_revision(self, item: PersonProfileRevision) -> None: ...
    def list_person_profile_revisions(self, person_key: str | None = None) -> list[PersonProfileRevision]: ...
    def append_person_search_plan_revision(self, item: PersonSearchPlanRevision) -> None: ...
    def list_person_search_plan_revisions(self, person_key: str | None = None) -> list[PersonSearchPlanRevision]: ...
    def append_person_profile_import_run(self, item: PersonProfileImportRun) -> None: ...
    def list_person_profile_import_runs(self) -> list[PersonProfileImportRun]: ...
    def append_person_profile_scan_run(self, item: PersonProfileScanRun) -> None: ...
    def list_person_profile_scan_runs(self, person_key: str | None = None) -> list[PersonProfileScanRun]: ...
    def append_person_update_bundle(self, item: PersonUpdateBundle) -> None: ...
    def list_person_update_bundles(self, person_key: str | None = None) -> list[PersonUpdateBundle]: ...
    def append_person_digest_batch(self, item: PersonDigestBatch) -> None: ...
    def list_person_digest_batches(self) -> list[PersonDigestBatch]: ...
    def append_profile_patch_review_batch(self, item: ProfilePatchReviewBatch) -> None: ...
    def list_profile_patch_review_batches(self) -> list[ProfilePatchReviewBatch]: ...
    def append_profile_patch_review_item(self, item: ProfilePatchReviewItem) -> None: ...
    def list_profile_patch_review_items(self, review_batch_id: str | None = None) -> list[ProfilePatchReviewItem]: ...
    def append_profile_patch_review_decision(self, item: ProfilePatchReviewDecision) -> None: ...
    def list_profile_patch_review_decisions(self, review_item_id: str | None = None) -> list[ProfilePatchReviewDecision]: ...
    def append_person_cognee_projection(self, item: PersonCogneeProjection) -> None: ...
    def list_person_cognee_projections(self, person_key: str | None = None) -> list[PersonCogneeProjection]: ...


class InMemoryLedger:
    """Append-only reference ledger used by unit tests and local demos."""

    def __init__(self):
        self._source_versions: dict[str, SourceDocumentVersion] = {}
        self._source_uri_index: dict[str, list[str]] = defaultdict(list)
        self._episodes: dict[str, Episode] = {}
        self._spans: dict[str, EvidenceSpan] = {}
        self._entities: dict[str, Entity] = {}
        self._runs: dict[str, ExtractionRun] = {}
        self._initial_review_batches: dict[str, InitialReviewBatch] = {}
        self._initial_review_items: dict[str, InitialReviewItem] = {}
        self._initial_review_decisions: dict[str, InitialReviewDecision] = {}
        self._assertions: dict[str, Assertion] = {}
        self._relations: dict[str, AssertionRelation] = {}
        self._annotations: dict[str, Annotation] = {}
        self._derived: dict[str, DerivedAssertionInputs] = {}
        self._signals: dict[str, Signal] = {}
        self._candidates: dict[str, CandidatePerson] = {}
        self._command_receipts: dict[str, CommandReceipt] = {}
        self._scan_runs: dict[str, ScanRun] = {}
        self._connector_attempts: dict[str, ConnectorAttempt] = {}
        self._discovery_candidates: dict[str, DiscoveryCandidate] = {}
        self._change_sets: dict[str, ChangeSet] = {}
        self._workflow_artifacts: dict[str, WorkflowArtifact] = {}
        self._connector_jobs: dict[str, ConnectorJob] = {}
        self._connector_job_command_index: dict[str, str] = {}
        self._connector_job_events: dict[str, ConnectorJobEvent] = {}
        self._connector_job_lock = Lock()
        self._person_keys: dict[str, PersonKeyRecord] = {}
        self._person_source_slices: dict[str, PersonSourceSlice] = {}
        self._person_profile_revisions: dict[str, PersonProfileRevision] = {}
        self._person_search_plan_revisions: dict[str, PersonSearchPlanRevision] = {}
        self._person_profile_import_runs: dict[str, PersonProfileImportRun] = {}
        self._person_profile_scan_runs: dict[str, PersonProfileScanRun] = {}
        self._person_update_bundles: dict[str, PersonUpdateBundle] = {}
        self._person_digest_batches: dict[str, PersonDigestBatch] = {}
        self._profile_patch_review_batches: dict[str, ProfilePatchReviewBatch] = {}
        self._profile_patch_review_items: dict[str, ProfilePatchReviewItem] = {}
        self._profile_patch_review_decisions: dict[str, ProfilePatchReviewDecision] = {}
        self._person_cognee_projections: dict[str, PersonCogneeProjection] = {}

    @staticmethod
    def _append(target: dict[str, object], key: str, value: object) -> None:
        if key in target:
            raise LedgerConflict(f"append-only id already exists: {key}")
        target[key] = deepcopy(value)

    @staticmethod
    def _get(target: dict[str, object], key: str):
        try:
            return deepcopy(target[key])
        except KeyError as exc:
            raise LedgerNotFound(key) from exc

    def append_source_version(self, item: SourceDocumentVersion) -> None:
        for existing_id in self._source_uri_index[item.source_uri]:
            existing = self._source_versions[existing_id]
            if existing.content_hash == item.content_hash:
                raise LedgerConflict(
                    f"source version already exists for uri/hash: {item.source_uri} {item.content_hash}"
                )
        self._append(self._source_versions, item.source_version_id, item)
        self._source_uri_index[item.source_uri].append(item.source_version_id)

    def get_source_version(self, source_version_id: str) -> SourceDocumentVersion:
        return self._get(self._source_versions, source_version_id)

    def find_source_versions(self, source_uri: str) -> list[SourceDocumentVersion]:
        return [deepcopy(self._source_versions[key]) for key in self._source_uri_index.get(source_uri, [])]

    def list_source_versions(self) -> list[SourceDocumentVersion]:
        return [deepcopy(item) for item in self._source_versions.values()]

    def append_episode(self, item: Episode) -> None:
        self._append(self._episodes, item.episode_id, item)

    def list_episodes(self) -> list[Episode]:
        return [deepcopy(item) for item in self._episodes.values()]

    def append_evidence_span(self, item: EvidenceSpan) -> None:
        if item.source_version_id not in self._source_versions:
            raise LedgerNotFound(item.source_version_id)
        self._append(self._spans, item.evidence_span_id, item)

    def get_evidence_span(self, evidence_span_id: str) -> EvidenceSpan:
        return self._get(self._spans, evidence_span_id)

    def list_evidence_spans(self, source_version_id: str | None = None) -> list[EvidenceSpan]:
        items = self._spans.values()
        if source_version_id is not None:
            items = [item for item in items if item.source_version_id == source_version_id]
        return [deepcopy(item) for item in items]

    def append_entity(self, item: Entity) -> None:
        self._append(self._entities, item.entity_id, item)

    def get_entity(self, entity_id: str) -> Entity:
        return self._get(self._entities, entity_id)

    def list_entities(self) -> list[Entity]:
        return [deepcopy(item) for item in self._entities.values()]

    def append_extraction_run(self, item: ExtractionRun) -> None:
        if item.source_version_id not in self._source_versions:
            raise LedgerNotFound(item.source_version_id)
        self._append(self._runs, item.extraction_run_id, item)

    def list_extraction_runs(self) -> list[ExtractionRun]:
        return [deepcopy(item) for item in self._runs.values()]

    def append_initial_review_batch(self, item: InitialReviewBatch) -> None:
        self._append(self._initial_review_batches, item.review_batch_id, item)

    def list_initial_review_batches(self) -> list[InitialReviewBatch]:
        return [deepcopy(item) for item in self._initial_review_batches.values()]

    def append_initial_review_item(self, item: InitialReviewItem) -> None:
        if item.review_batch_id not in self._initial_review_batches:
            raise LedgerNotFound(item.review_batch_id)
        if item.target_assertion_id not in self._assertions:
            raise LedgerNotFound(item.target_assertion_id)
        self._append(self._initial_review_items, item.review_item_id, item)

    def list_initial_review_items(self, review_batch_id: str | None = None) -> list[InitialReviewItem]:
        items = self._initial_review_items.values()
        if review_batch_id is not None:
            items = [item for item in items if item.review_batch_id == review_batch_id]
        return [deepcopy(item) for item in items]

    def append_initial_review_decision(self, item: InitialReviewDecision) -> None:
        if item.review_item_id not in self._initial_review_items:
            raise LedgerNotFound(item.review_item_id)
        self._append(self._initial_review_decisions, item.review_decision_id, item)

    def list_initial_review_decisions(self, review_item_id: str | None = None) -> list[InitialReviewDecision]:
        items = self._initial_review_decisions.values()
        if review_item_id is not None:
            items = [item for item in items if item.review_item_id == review_item_id]
        return [deepcopy(item) for item in items]

    def append_assertion(self, item: Assertion) -> None:
        self._append(self._assertions, item.assertion_id, item)

    def get_assertion(self, assertion_id: str) -> Assertion:
        return self._get(self._assertions, assertion_id)

    def list_assertions(self, subject_entity_id: str | None = None) -> list[Assertion]:
        items = self._assertions.values()
        if subject_entity_id is not None:
            items = [item for item in items if item.subject_entity_id == subject_entity_id]
        return [deepcopy(item) for item in items]

    def append_assertion_relation(self, item: AssertionRelation) -> None:
        if item.from_assertion_id not in self._assertions:
            raise LedgerNotFound(item.from_assertion_id)
        if item.to_assertion_id not in self._assertions:
            raise LedgerNotFound(item.to_assertion_id)
        self._append(self._relations, item.assertion_relation_id, item)

    def list_assertion_relations(self) -> list[AssertionRelation]:
        return [deepcopy(item) for item in self._relations.values()]

    def append_annotation(self, item: Annotation) -> None:
        if item.target_assertion_id not in self._assertions:
            raise LedgerNotFound(item.target_assertion_id)
        self._append(self._annotations, item.annotation_id, item)

    def list_annotations(self, target_assertion_id: str | None = None) -> list[Annotation]:
        items = self._annotations.values()
        if target_assertion_id is not None:
            items = [item for item in items if item.target_assertion_id == target_assertion_id]
        return [deepcopy(item) for item in items]

    def append_derived_inputs(self, item: DerivedAssertionInputs) -> None:
        if item.derived_assertion_id not in self._assertions:
            raise LedgerNotFound(item.derived_assertion_id)
        for assertion_id in item.input_assertion_ids:
            if assertion_id not in self._assertions:
                raise LedgerNotFound(assertion_id)
        self._append(self._derived, item.derived_assertion_id, item)

    def get_derived_inputs(self, derived_assertion_id: str) -> DerivedAssertionInputs | None:
        item = self._derived.get(derived_assertion_id)
        return deepcopy(item) if item else None

    def list_derived_inputs(self) -> list[DerivedAssertionInputs]:
        return [deepcopy(item) for item in self._derived.values()]

    def append_signal(self, item: Signal) -> None:
        self._append(self._signals, item.signal_id, item)

    def list_signals(self) -> list[Signal]:
        return [deepcopy(item) for item in self._signals.values()]

    def append_candidate(self, item: CandidatePerson) -> None:
        self._append(self._candidates, item.candidate_id, item)

    def list_candidates(self) -> list[CandidatePerson]:
        return [deepcopy(item) for item in self._candidates.values()]

    def append_command_receipt(self, item: CommandReceipt) -> None:
        self._append(self._command_receipts, item.command_id, item)

    def get_command_receipt(self, command_id: str) -> CommandReceipt | None:
        item = self._command_receipts.get(command_id)
        return deepcopy(item) if item else None

    def append_scan_run(self, item: ScanRun) -> None:
        self._append(self._scan_runs, item.scan_run_id, item)

    def list_scan_runs(self) -> list[ScanRun]:
        return [deepcopy(item) for item in self._scan_runs.values()]

    def append_connector_attempt(self, item: ConnectorAttempt) -> None:
        if item.scan_run_id not in self._scan_runs:
            raise LedgerNotFound(item.scan_run_id)
        self._append(self._connector_attempts, item.connector_attempt_id, item)

    def list_connector_attempts(self, scan_run_id: str | None = None) -> list[ConnectorAttempt]:
        items = self._connector_attempts.values()
        if scan_run_id:
            items = [item for item in items if item.scan_run_id == scan_run_id]
        return [deepcopy(item) for item in items]

    def append_discovery_candidate(self, item: DiscoveryCandidate) -> None:
        if item.scan_run_id not in self._scan_runs:
            raise LedgerNotFound(item.scan_run_id)
        self._append(self._discovery_candidates, item.discovery_candidate_id, item)

    def list_discovery_candidates(self, scan_run_id: str | None = None) -> list[DiscoveryCandidate]:
        items = self._discovery_candidates.values()
        if scan_run_id:
            items = [item for item in items if item.scan_run_id == scan_run_id]
        return [deepcopy(item) for item in items]

    def append_change_set(self, item: ChangeSet) -> None:
        if item.scan_run_id not in self._scan_runs:
            raise LedgerNotFound(item.scan_run_id)
        self._append(self._change_sets, item.change_set_id, item)

    def list_change_sets(self, scan_run_id: str | None = None) -> list[ChangeSet]:
        items = self._change_sets.values()
        if scan_run_id:
            items = [item for item in items if item.scan_run_id == scan_run_id]
        return [deepcopy(item) for item in items]

    def append_workflow_artifact(self, item: WorkflowArtifact) -> None:
        if item.scan_run_id not in self._scan_runs:
            raise LedgerNotFound(item.scan_run_id)
        self._append(self._workflow_artifacts, item.artifact_id, item)

    def list_workflow_artifacts(self, scan_run_id: str | None = None) -> list[WorkflowArtifact]:
        items = self._workflow_artifacts.values()
        if scan_run_id:
            items = [item for item in items if item.scan_run_id == scan_run_id]
        return [deepcopy(item) for item in items]

    def append_connector_job(self, item: ConnectorJob) -> None:
        with self._connector_job_lock:
            if item.command_id in self._connector_job_command_index:
                raise LedgerConflict(f"connector job command already exists: {item.command_id}")
            self._append(self._connector_jobs, item.connector_job_id, item)
            self._connector_job_command_index[item.command_id] = item.connector_job_id

    def get_connector_job(self, connector_job_id: str) -> ConnectorJob:
        return self._get(self._connector_jobs, connector_job_id)

    def find_connector_job_by_command_id(self, command_id: str) -> ConnectorJob | None:
        connector_job_id = self._connector_job_command_index.get(command_id)
        return self._get(self._connector_jobs, connector_job_id) if connector_job_id else None

    def list_connector_jobs(self) -> list[ConnectorJob]:
        return [deepcopy(item) for item in self._connector_jobs.values()]

    def append_connector_job_event(self, item: ConnectorJobEvent) -> None:
        with self._connector_job_lock:
            if item.connector_job_id not in self._connector_jobs:
                raise LedgerNotFound(item.connector_job_id)
            self._append(self._connector_job_events, item.connector_job_event_id, item)

    def list_connector_job_events(self, connector_job_id: str | None = None) -> list[ConnectorJobEvent]:
        items = self._connector_job_events.values()
        if connector_job_id:
            items = [item for item in items if item.connector_job_id == connector_job_id]
        return [deepcopy(item) for item in items]

    def claim_next_connector_job(
        self,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> tuple[ConnectorJob, ConnectorJobEvent] | None:
        with self._connector_job_lock:
            for job in sorted(self._connector_jobs.values(), key=lambda item: item.created_at):
                events = [item for item in self._connector_job_events.values() if item.connector_job_id == job.connector_job_id]
                last = events[-1] if events else None
                if last and last.event_type in {"succeeded", "dead_lettered"}:
                    continue
                if job.not_before > now:
                    continue
                if last and last.event_type in {"retry_scheduled", "deferred"} and last.next_attempt_at and last.next_attempt_at > now:
                    continue
                if last and last.event_type == "claimed" and last.lease_expires_at and last.lease_expires_at > now:
                    continue
                attempt = (
                    last.attempt
                    if last and last.event_type == "deferred"
                    else sum(item.event_type == "claimed" for item in events) + 1
                )
                event = ConnectorJobEvent(
                    connector_job_id=job.connector_job_id,
                    event_type="claimed",
                    worker_id=worker_id,
                    attempt=attempt,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    occurred_at=now,
                )
                self._append(self._connector_job_events, event.connector_job_event_id, event)
                return deepcopy(job), deepcopy(event)
        return None

    def append_person_key(self, item: PersonKeyRecord) -> None:
        if any(existing.person_key == item.person_key for existing in self._person_keys.values()):
            raise LedgerConflict(f"person_key already exists: {item.person_key}")
        if any(existing.entity_id == item.entity_id for existing in self._person_keys.values()):
            raise LedgerConflict(f"entity already has person_key: {item.entity_id}")
        self._append(self._person_keys, item.person_key_id, item)

    def list_person_keys(self) -> list[PersonKeyRecord]:
        return [deepcopy(item) for item in self._person_keys.values()]

    def append_person_source_slice(self, item: PersonSourceSlice) -> None:
        self._append(self._person_source_slices, item.person_source_slice_id, item)

    def list_person_source_slices(self, person_key: str | None = None) -> list[PersonSourceSlice]:
        items = self._person_source_slices.values()
        if person_key:
            items = [item for item in items if item.person_key == person_key]
        return [deepcopy(item) for item in items]

    def append_person_profile_revision(self, item: PersonProfileRevision) -> None:
        self._append(self._person_profile_revisions, item.profile_revision_id, item)

    def list_person_profile_revisions(self, person_key: str | None = None) -> list[PersonProfileRevision]:
        items = self._person_profile_revisions.values()
        if person_key:
            items = [item for item in items if item.person_key == person_key]
        return [deepcopy(item) for item in items]

    def append_person_search_plan_revision(self, item: PersonSearchPlanRevision) -> None:
        self._append(self._person_search_plan_revisions, item.search_plan_revision_id, item)

    def list_person_search_plan_revisions(self, person_key: str | None = None) -> list[PersonSearchPlanRevision]:
        items = self._person_search_plan_revisions.values()
        if person_key:
            items = [item for item in items if item.person_key == person_key]
        return [deepcopy(item) for item in items]

    def append_person_profile_import_run(self, item: PersonProfileImportRun) -> None:
        if any(existing.command_id == item.command_id for existing in self._person_profile_import_runs.values()):
            raise LedgerConflict(f"profile import command already exists: {item.command_id}")
        self._append(self._person_profile_import_runs, item.import_run_id, item)

    def list_person_profile_import_runs(self) -> list[PersonProfileImportRun]:
        return [deepcopy(item) for item in self._person_profile_import_runs.values()]

    def append_person_profile_scan_run(self, item: PersonProfileScanRun) -> None:
        if any(existing.command_id == item.command_id for existing in self._person_profile_scan_runs.values()):
            raise LedgerConflict(f"profile scan command already exists: {item.command_id}")
        self._append(self._person_profile_scan_runs, item.person_profile_scan_run_id, item)

    def list_person_profile_scan_runs(self, person_key: str | None = None) -> list[PersonProfileScanRun]:
        items = self._person_profile_scan_runs.values()
        if person_key:
            items = [item for item in items if item.person_key == person_key]
        return [deepcopy(item) for item in items]

    def append_person_update_bundle(self, item: PersonUpdateBundle) -> None:
        self._append(self._person_update_bundles, item.bundle_id, item)

    def list_person_update_bundles(self, person_key: str | None = None) -> list[PersonUpdateBundle]:
        items = self._person_update_bundles.values()
        if person_key:
            items = [item for item in items if item.person_key == person_key]
        return [deepcopy(item) for item in items]

    def append_person_digest_batch(self, item: PersonDigestBatch) -> None:
        self._append(self._person_digest_batches, item.digest_batch_id, item)

    def list_person_digest_batches(self) -> list[PersonDigestBatch]:
        return [deepcopy(item) for item in self._person_digest_batches.values()]

    def append_profile_patch_review_batch(self, item: ProfilePatchReviewBatch) -> None:
        self._append(self._profile_patch_review_batches, item.review_batch_id, item)

    def list_profile_patch_review_batches(self) -> list[ProfilePatchReviewBatch]:
        return [deepcopy(item) for item in self._profile_patch_review_batches.values()]

    def append_profile_patch_review_item(self, item: ProfilePatchReviewItem) -> None:
        if item.review_batch_id not in self._profile_patch_review_batches:
            raise LedgerNotFound(item.review_batch_id)
        self._append(self._profile_patch_review_items, item.review_item_id, item)

    def list_profile_patch_review_items(self, review_batch_id: str | None = None) -> list[ProfilePatchReviewItem]:
        items = self._profile_patch_review_items.values()
        if review_batch_id:
            items = [item for item in items if item.review_batch_id == review_batch_id]
        return [deepcopy(item) for item in items]

    def append_profile_patch_review_decision(self, item: ProfilePatchReviewDecision) -> None:
        if item.review_item_id not in self._profile_patch_review_items:
            raise LedgerNotFound(item.review_item_id)
        if any(existing.command_id == item.command_id for existing in self._profile_patch_review_decisions.values()):
            raise LedgerConflict(f"profile review command already exists: {item.command_id}")
        self._append(self._profile_patch_review_decisions, item.review_decision_id, item)

    def list_profile_patch_review_decisions(self, review_item_id: str | None = None) -> list[ProfilePatchReviewDecision]:
        items = self._profile_patch_review_decisions.values()
        if review_item_id:
            items = [item for item in items if item.review_item_id == review_item_id]
        return [deepcopy(item) for item in items]

    def append_person_cognee_projection(self, item: PersonCogneeProjection) -> None:
        self._append(self._person_cognee_projections, item.projection_id, item)

    def list_person_cognee_projections(self, person_key: str | None = None) -> list[PersonCogneeProjection]:
        items = self._person_cognee_projections.values()
        if person_key:
            items = [item for item in items if item.person_key == person_key]
        return [deepcopy(item) for item in items]
