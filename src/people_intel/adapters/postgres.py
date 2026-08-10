from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from people_intel.ledger import LedgerConflict, LedgerNotFound
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
    OntologyDefinition,
    Signal,
    SourceDocumentVersion,
    ScanRun,
    ConnectorAttempt,
    ConnectorJob,
    ConnectorJobEvent,
    DiscoveryCandidate,
    ChangeSet,
    WorkflowArtifact,
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


T = TypeVar("T")


def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("PostgresLedger requires the 'postgres' optional dependency") from exc
    return psycopg, dict_row


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False)


class PostgresLedger:
    """PostgreSQL implementation of the append-only ledger protocol."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    def initialize_schema(self, schema_path: str | Path | None = None) -> None:
        psycopg, _ = _psycopg()
        project_root = Path(
            os.environ.get("PEOPLE_INTEL_PROJECT_ROOT", Path(__file__).resolve().parents[3])
        )
        path = Path(schema_path) if schema_path else project_root / "db" / "schema.sql"
        sql = path.read_text(encoding="utf-8")
        with psycopg.connect(self.database_url) as conn:
            conn.execute(sql)

    def register_ontology(self, definition: OntologyDefinition) -> None:
        psycopg, _ = _psycopg()
        with psycopg.connect(self.database_url) as conn:
            for predicate in definition.predicates:
                expected = predicate.model_dump(mode="json")
                conn.execute(
                    """
                    INSERT INTO predicate_definitions(ontology_version, predicate_id, definition)
                    VALUES (%s,%s,%s::jsonb)
                    ON CONFLICT (ontology_version, predicate_id) DO NOTHING
                    """,
                    (definition.version, predicate.predicate_id, _json(expected)),
                )
                stored = conn.execute(
                    """
                    SELECT definition FROM predicate_definitions
                    WHERE ontology_version=%s AND predicate_id=%s
                    """,
                    (definition.version, predicate.predicate_id),
                ).fetchone()[0]
                if stored != expected:
                    raise RuntimeError(
                        f"ontology version collision for {definition.version}/{predicate.predicate_id}"
                    )

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        psycopg, _ = _psycopg()
        try:
            with psycopg.connect(self.database_url) as conn:
                conn.execute(sql, params)
        except Exception as exc:
            if "duplicate key" in str(exc).lower() or "unique constraint" in str(exc).lower():
                raise LedgerConflict(str(exc)) from exc
            raise

    def _fetchone(self, sql: str, params: tuple[Any, ...], model: type[T]) -> T:
        psycopg, dict_row = _psycopg()
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            raise LedgerNotFound(params[0] if params else "record")
        return model.model_validate(dict(row))

    def _fetchall(self, sql: str, params: tuple[Any, ...], model: type[T]) -> list[T]:
        psycopg, dict_row = _psycopg()
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [model.model_validate(dict(row)) for row in rows]

    def append_source_version(self, item: SourceDocumentVersion) -> None:
        self._execute(
            """
            INSERT INTO source_document_versions(
              source_version_id, source_uri, source_type, media_type, content_hash,
              raw_object_ref, normalized_markdown_ref, published_at, retrieved_at,
              source_identity, fetch_method, rights_scope, previous_version_id, metadata
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                item.source_version_id, item.source_uri, item.source_type, item.media_type,
                item.content_hash, item.raw_object_ref, item.normalized_markdown_ref,
                item.published_at, item.retrieved_at, item.source_identity, item.fetch_method,
                item.rights_scope, item.previous_version_id, _json(item.metadata),
            ),
        )

    def get_source_version(self, source_version_id: str) -> SourceDocumentVersion:
        return self._fetchone(
            "SELECT * FROM source_document_versions WHERE source_version_id=%s",
            (source_version_id,), SourceDocumentVersion,
        )

    def find_source_versions(self, source_uri: str) -> list[SourceDocumentVersion]:
        return self._fetchall(
            "SELECT * FROM source_document_versions WHERE source_uri=%s ORDER BY retrieved_at",
            (source_uri,), SourceDocumentVersion,
        )

    def list_source_versions(self) -> list[SourceDocumentVersion]:
        return self._fetchall(
            "SELECT * FROM source_document_versions ORDER BY retrieved_at",
            (), SourceDocumentVersion,
        )

    def append_episode(self, item: Episode) -> None:
        self._execute(
            """
            INSERT INTO episodes(episode_id, episode_type, source_version_ids, entity_candidate_ids,
              scan_run_id, occurred_at, observed_at, actor_id, context)
            VALUES (%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s,%s,%s::jsonb)
            """,
            (
                item.episode_id, item.episode_type, _json(item.source_version_ids),
                _json(item.entity_candidate_ids), item.scan_run_id,
                _json(item.occurred_at) if item.occurred_at else None,
                item.observed_at, item.actor_id, _json(item.context),
            ),
        )

    def list_episodes(self) -> list[Episode]:
        return self._fetchall("SELECT * FROM episodes ORDER BY observed_at", (), Episode)

    def append_evidence_span(self, item: EvidenceSpan) -> None:
        self._execute(
            """
            INSERT INTO evidence_spans(evidence_span_id, source_version_id, locator_type, locator,
              quote, quote_hash, normalized_version_ref, created_at)
            VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
            """,
            (
                item.evidence_span_id, item.source_version_id, item.locator_type,
                _json(item.locator), item.quote, item.quote_hash,
                item.normalized_version_ref, item.created_at,
            ),
        )

    def get_evidence_span(self, evidence_span_id: str) -> EvidenceSpan:
        return self._fetchone(
            "SELECT * FROM evidence_spans WHERE evidence_span_id=%s",
            (evidence_span_id,), EvidenceSpan,
        )

    def list_evidence_spans(self, source_version_id: str | None = None) -> list[EvidenceSpan]:
        if source_version_id:
            return self._fetchall(
                "SELECT * FROM evidence_spans WHERE source_version_id=%s ORDER BY created_at",
                (source_version_id,), EvidenceSpan,
            )
        return self._fetchall("SELECT * FROM evidence_spans ORDER BY created_at", (), EvidenceSpan)

    def append_entity(self, item: Entity) -> None:
        self._execute(
            """
            INSERT INTO entities(entity_id, entity_type, canonical_name, aliases, metadata, created_at)
            VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s)
            """,
            (item.entity_id, item.entity_type, item.canonical_name, _json(item.aliases), _json(item.metadata), item.created_at),
        )

    def get_entity(self, entity_id: str) -> Entity:
        return self._fetchone("SELECT * FROM entities WHERE entity_id=%s", (entity_id,), Entity)

    def list_entities(self) -> list[Entity]:
        return self._fetchall("SELECT * FROM entities ORDER BY created_at", (), Entity)

    def append_extraction_run(self, item: ExtractionRun) -> None:
        self._execute(
            """
            INSERT INTO extraction_runs(extraction_run_id, source_version_id, extractor,
              extractor_version, ontology_version, prompt_hash, model_name, started_at,
              completed_at, status, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                item.extraction_run_id, item.source_version_id, item.extractor,
                item.extractor_version, item.ontology_version, item.prompt_hash,
                item.model_name, item.started_at, item.completed_at, item.status,
                _json(item.metadata),
            ),
        )

    def list_extraction_runs(self) -> list[ExtractionRun]:
        return self._fetchall(
            "SELECT * FROM extraction_runs ORDER BY started_at", (), ExtractionRun
        )

    def append_initial_review_batch(self, item: InitialReviewBatch) -> None:
        self._execute(
            "INSERT INTO initial_review_batches(review_batch_id, payload, created_at) VALUES (%s,%s::jsonb,%s)",
            (item.review_batch_id, _json(item), item.created_at),
        )

    def list_initial_review_batches(self) -> list[InitialReviewBatch]:
        return self._fetch_payloads("SELECT payload FROM initial_review_batches ORDER BY created_at", InitialReviewBatch)

    def append_initial_review_item(self, item: InitialReviewItem) -> None:
        self._execute(
            "INSERT INTO initial_review_items(review_item_id, review_batch_id, payload, created_at) VALUES (%s,%s,%s::jsonb,%s)",
            (item.review_item_id, item.review_batch_id, _json(item), item.created_at),
        )

    def list_initial_review_items(self, review_batch_id: str | None = None) -> list[InitialReviewItem]:
        if review_batch_id:
            return self._fetch_payloads("SELECT payload FROM initial_review_items WHERE review_batch_id=%s ORDER BY created_at", InitialReviewItem, (review_batch_id,))
        return self._fetch_payloads("SELECT payload FROM initial_review_items ORDER BY created_at", InitialReviewItem)

    def append_initial_review_decision(self, item: InitialReviewDecision) -> None:
        self._execute(
            "INSERT INTO initial_review_decisions(review_decision_id, review_item_id, payload, created_at) VALUES (%s,%s,%s::jsonb,%s)",
            (item.review_decision_id, item.review_item_id, _json(item), item.created_at),
        )

    def list_initial_review_decisions(self, review_item_id: str | None = None) -> list[InitialReviewDecision]:
        if review_item_id:
            return self._fetch_payloads("SELECT payload FROM initial_review_decisions WHERE review_item_id=%s ORDER BY created_at", InitialReviewDecision, (review_item_id,))
        return self._fetch_payloads("SELECT payload FROM initial_review_decisions ORDER BY created_at", InitialReviewDecision)

    def append_assertion(self, item: Assertion) -> None:
        self._execute(
            """
            INSERT INTO assertions(assertion_id, subject_entity_id, predicate_id, object_value,
              polarity, epistemic_type, initial_status, valid_time, transaction_time,
              evidence_span_ids, confidence, extraction_run_id, ontology_version,
              review_required, created_at, metadata)
            VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                item.assertion_id, item.subject_entity_id, item.predicate_id, _json(item.object),
                item.polarity, item.epistemic_type, item.status, _json(item.valid_time),
                _json(item.transaction_time), _json(item.evidence_span_ids), item.confidence,
                item.extraction_run_id, item.ontology_version, item.review_required,
                item.created_at, _json(item.metadata),
            ),
        )

    @staticmethod
    def _assertion_select(where: str) -> str:
        return f"""
          SELECT assertion_id, subject_entity_id, predicate_id, object_value AS object,
            polarity, epistemic_type, initial_status AS status, valid_time,
            transaction_time, evidence_span_ids, confidence, extraction_run_id,
            ontology_version, review_required, created_at, metadata
          FROM assertions {where}
        """

    def get_assertion(self, assertion_id: str) -> Assertion:
        return self._fetchone(self._assertion_select("WHERE assertion_id=%s"), (assertion_id,), Assertion)

    def list_assertions(self, subject_entity_id: str | None = None) -> list[Assertion]:
        if subject_entity_id:
            return self._fetchall(
                self._assertion_select("WHERE subject_entity_id=%s ORDER BY created_at"),
                (subject_entity_id,), Assertion,
            )
        return self._fetchall(self._assertion_select("ORDER BY created_at"), (), Assertion)

    def append_assertion_relation(self, item: AssertionRelation) -> None:
        self._execute(
            """
            INSERT INTO assertion_relations(assertion_relation_id, from_assertion_id,
              relation_type, to_assertion_id, created_at, metadata)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                item.assertion_relation_id, item.from_assertion_id, item.relation_type,
                item.to_assertion_id, item.created_at, _json(item.metadata),
            ),
        )

    def list_assertion_relations(self) -> list[AssertionRelation]:
        return self._fetchall("SELECT * FROM assertion_relations ORDER BY created_at", (), AssertionRelation)

    def append_annotation(self, item: Annotation) -> None:
        self._execute(
            """
            INSERT INTO annotations(annotation_id, target_assertion_id, action,
              replacement_assertion_id, reason, actor_id, created_at, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                item.annotation_id, item.target_assertion_id, item.action,
                item.replacement_assertion_id, item.reason, item.actor_id,
                item.created_at, _json(item.metadata),
            ),
        )

    def list_annotations(self, target_assertion_id: str | None = None) -> list[Annotation]:
        if target_assertion_id:
            return self._fetchall(
                "SELECT * FROM annotations WHERE target_assertion_id=%s ORDER BY created_at",
                (target_assertion_id,), Annotation,
            )
        return self._fetchall("SELECT * FROM annotations ORDER BY created_at", (), Annotation)

    def append_derived_inputs(self, item: DerivedAssertionInputs) -> None:
        self._execute(
            """
            INSERT INTO derived_assertion_inputs(derived_assertion_id, input_assertion_ids,
              rule_id, rule_version, model_name, prompt_hash, created_at)
            VALUES (%s,%s::jsonb,%s,%s,%s,%s,%s)
            """,
            (
                item.derived_assertion_id, _json(item.input_assertion_ids), item.rule_id,
                item.rule_version, item.model_name, item.prompt_hash, item.created_at,
            ),
        )

    def get_derived_inputs(self, derived_assertion_id: str) -> DerivedAssertionInputs | None:
        try:
            return self._fetchone(
                "SELECT * FROM derived_assertion_inputs WHERE derived_assertion_id=%s",
                (derived_assertion_id,), DerivedAssertionInputs,
            )
        except LedgerNotFound:
            return None

    def list_derived_inputs(self) -> list[DerivedAssertionInputs]:
        return self._fetchall(
            "SELECT * FROM derived_assertion_inputs ORDER BY created_at", (), DerivedAssertionInputs
        )

    def append_signal(self, item: Signal) -> None:
        self._execute(
            "INSERT INTO signals(signal_id, payload, created_at) VALUES (%s,%s::jsonb,%s)",
            (item.signal_id, _json(item), item.created_at),
        )

    def list_signals(self) -> list[Signal]:
        return self._fetch_payloads("SELECT payload FROM signals ORDER BY created_at", Signal)

    def append_candidate(self, item: CandidatePerson) -> None:
        self._execute(
            "INSERT INTO candidate_people(candidate_id, payload, created_at) VALUES (%s,%s::jsonb,%s)",
            (item.candidate_id, _json(item), item.created_at),
        )

    def list_candidates(self) -> list[CandidatePerson]:
        return self._fetch_payloads("SELECT payload FROM candidate_people ORDER BY created_at", CandidatePerson)

    def append_command_receipt(self, item: CommandReceipt) -> None:
        self._execute(
            "INSERT INTO command_receipts(command_id, payload, processed_at) VALUES (%s,%s::jsonb,%s)",
            (item.command_id, _json(item), item.processed_at),
        )

    def get_command_receipt(self, command_id: str) -> CommandReceipt | None:
        psycopg, dict_row = _psycopg()
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT payload FROM command_receipts WHERE command_id=%s", (command_id,)
            ).fetchone()
        return CommandReceipt.model_validate(row["payload"]) if row else None

    def append_scan_run(self, item: ScanRun) -> None:
        self._execute(
            "INSERT INTO scan_runs(scan_run_id, payload, created_at) VALUES (%s,%s::jsonb,%s)",
            (item.scan_run_id, _json(item), item.started_at),
        )

    def list_scan_runs(self) -> list[ScanRun]:
        return self._fetch_payloads("SELECT payload FROM scan_runs ORDER BY created_at", ScanRun)

    def append_connector_attempt(self, item: ConnectorAttempt) -> None:
        self._execute(
            "INSERT INTO connector_attempts(connector_attempt_id, scan_run_id, payload, created_at) VALUES (%s,%s,%s::jsonb,%s)",
            (item.connector_attempt_id, item.scan_run_id, _json(item), item.created_at),
        )

    def list_connector_attempts(self, scan_run_id: str | None = None) -> list[ConnectorAttempt]:
        if scan_run_id:
            return self._fetch_payloads("SELECT payload FROM connector_attempts WHERE scan_run_id=%s ORDER BY created_at", ConnectorAttempt, (scan_run_id,))
        return self._fetch_payloads("SELECT payload FROM connector_attempts ORDER BY created_at", ConnectorAttempt)

    def append_discovery_candidate(self, item: DiscoveryCandidate) -> None:
        self._execute(
            "INSERT INTO discovery_candidates(discovery_candidate_id, scan_run_id, payload, created_at) VALUES (%s,%s,%s::jsonb,%s)",
            (item.discovery_candidate_id, item.scan_run_id, _json(item), item.created_at),
        )

    def list_discovery_candidates(self, scan_run_id: str | None = None) -> list[DiscoveryCandidate]:
        if scan_run_id:
            return self._fetch_payloads("SELECT payload FROM discovery_candidates WHERE scan_run_id=%s ORDER BY created_at", DiscoveryCandidate, (scan_run_id,))
        return self._fetch_payloads("SELECT payload FROM discovery_candidates ORDER BY created_at", DiscoveryCandidate)

    def append_change_set(self, item: ChangeSet) -> None:
        self._execute(
            "INSERT INTO change_sets(change_set_id, scan_run_id, payload, created_at) VALUES (%s,%s,%s::jsonb,%s)",
            (item.change_set_id, item.scan_run_id, _json(item), item.created_at),
        )

    def list_change_sets(self, scan_run_id: str | None = None) -> list[ChangeSet]:
        if scan_run_id:
            return self._fetch_payloads("SELECT payload FROM change_sets WHERE scan_run_id=%s ORDER BY created_at", ChangeSet, (scan_run_id,))
        return self._fetch_payloads("SELECT payload FROM change_sets ORDER BY created_at", ChangeSet)

    def append_workflow_artifact(self, item: WorkflowArtifact) -> None:
        self._execute(
            "INSERT INTO workflow_artifacts(artifact_id, scan_run_id, payload, created_at) VALUES (%s,%s,%s::jsonb,%s)",
            (item.artifact_id, item.scan_run_id, _json(item), item.created_at),
        )

    def list_workflow_artifacts(self, scan_run_id: str | None = None) -> list[WorkflowArtifact]:
        if scan_run_id:
            return self._fetch_payloads("SELECT payload FROM workflow_artifacts WHERE scan_run_id=%s ORDER BY created_at", WorkflowArtifact, (scan_run_id,))
        return self._fetch_payloads("SELECT payload FROM workflow_artifacts ORDER BY created_at", WorkflowArtifact)

    def append_connector_job(self, item: ConnectorJob) -> None:
        self._execute(
            """
            INSERT INTO connector_jobs(connector_job_id, command_id, max_attempts, not_before, payload, created_at)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s)
            """,
            (item.connector_job_id, item.command_id, item.max_attempts, item.not_before, _json(item), item.created_at),
        )

    def get_connector_job(self, connector_job_id: str) -> ConnectorJob:
        values = self._fetch_payloads(
            "SELECT payload FROM connector_jobs WHERE connector_job_id=%s",
            ConnectorJob,
            (connector_job_id,),
        )
        if not values:
            raise LedgerNotFound(connector_job_id)
        return values[0]

    def find_connector_job_by_command_id(self, command_id: str) -> ConnectorJob | None:
        values = self._fetch_payloads(
            "SELECT payload FROM connector_jobs WHERE command_id=%s",
            ConnectorJob,
            (command_id,),
        )
        return values[0] if values else None

    def list_connector_jobs(self) -> list[ConnectorJob]:
        return self._fetch_payloads("SELECT payload FROM connector_jobs ORDER BY created_at", ConnectorJob)

    def append_connector_job_event(self, item: ConnectorJobEvent) -> None:
        self._execute(
            """
            INSERT INTO connector_job_events(
              connector_job_event_id, connector_job_id, event_type, worker_id, attempt,
              lease_expires_at, next_attempt_at, payload, occurred_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            """,
            (
                item.connector_job_event_id, item.connector_job_id, item.event_type,
                item.worker_id, item.attempt, item.lease_expires_at, item.next_attempt_at,
                _json(item), item.occurred_at,
            ),
        )

    def list_connector_job_events(self, connector_job_id: str | None = None) -> list[ConnectorJobEvent]:
        if connector_job_id:
            return self._fetch_payloads(
                "SELECT payload FROM connector_job_events WHERE connector_job_id=%s ORDER BY event_sequence",
                ConnectorJobEvent,
                (connector_job_id,),
            )
        return self._fetch_payloads(
            "SELECT payload FROM connector_job_events ORDER BY event_sequence",
            ConnectorJobEvent,
        )

    def claim_next_connector_job(
        self,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> tuple[ConnectorJob, ConnectorJobEvent] | None:
        psycopg, dict_row = _psycopg()
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT j.connector_job_id, j.payload
                FROM connector_jobs j
                LEFT JOIN LATERAL (
                  SELECT e.event_type, e.lease_expires_at, e.next_attempt_at
                  FROM connector_job_events e
                  WHERE e.connector_job_id = j.connector_job_id
                  ORDER BY e.event_sequence DESC
                  LIMIT 1
                ) last ON TRUE
                WHERE j.not_before <= %s
                  AND (
                    last.event_type IS NULL
                    OR last.event_type = 'queued'
                    OR (last.event_type IN ('retry_scheduled','deferred') AND last.next_attempt_at <= %s)
                    OR (last.event_type = 'claimed' AND last.lease_expires_at <= %s)
                  )
                ORDER BY j.created_at
                FOR UPDATE OF j SKIP LOCKED
                LIMIT 1
                """,
                (now, now, now),
            ).fetchone()
            if row is None:
                return None
            attempt_row = conn.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE event_type='claimed') AS claim_count,
                  (ARRAY_AGG(attempt ORDER BY event_sequence DESC)
                    FILTER (WHERE event_type='deferred'))[1] AS deferred_attempt,
                  (ARRAY_AGG(event_type ORDER BY event_sequence DESC))[1] AS last_event_type
                FROM connector_job_events
                WHERE connector_job_id=%s
                """,
                (row["connector_job_id"],),
            ).fetchone()
            attempt = (
                attempt_row["deferred_attempt"]
                if attempt_row["last_event_type"] == "deferred"
                else attempt_row["claim_count"] + 1
            )
            job = ConnectorJob.model_validate(row["payload"])
            event = ConnectorJobEvent(
                connector_job_id=job.connector_job_id,
                event_type="claimed",
                worker_id=worker_id,
                attempt=attempt,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                occurred_at=now,
            )
            conn.execute(
                """
                INSERT INTO connector_job_events(
                  connector_job_event_id, connector_job_id, event_type, worker_id, attempt,
                  lease_expires_at, next_attempt_at, payload, occurred_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                """,
                (
                    event.connector_job_event_id, event.connector_job_id, event.event_type,
                    event.worker_id, event.attempt, event.lease_expires_at,
                    event.next_attempt_at, _json(event), event.occurred_at,
                ),
            )
        return job, event

    def _append_payload_record(
        self,
        table: str,
        id_column: str,
        record_id: str,
        item: Any,
        *,
        extra_columns: tuple[str, ...] = (),
        extra_values: tuple[Any, ...] = (),
    ) -> None:
        columns = (id_column, *extra_columns, "payload", "created_at")
        placeholders = ["%s"] * (len(columns) - 2) + ["%s::jsonb", "%s"]
        self._execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join(placeholders)})",
            (record_id, *extra_values, _json(item), item.created_at),
        )

    def append_person_key(self, item: PersonKeyRecord) -> None:
        self._append_payload_record(
            "person_keys", "person_key_id", item.person_key_id, item,
            extra_columns=("person_key", "entity_id"),
            extra_values=(item.person_key, item.entity_id),
        )

    def list_person_keys(self) -> list[PersonKeyRecord]:
        return self._fetch_payloads("SELECT payload FROM person_keys ORDER BY created_at", PersonKeyRecord)

    def append_person_source_slice(self, item: PersonSourceSlice) -> None:
        self._append_payload_record(
            "person_source_slices", "person_source_slice_id", item.person_source_slice_id, item,
            extra_columns=("person_key", "source_version_id", "evidence_span_id"),
            extra_values=(item.person_key, item.source_version_id, item.evidence_span_id),
        )

    def list_person_source_slices(self, person_key: str | None = None) -> list[PersonSourceSlice]:
        if person_key:
            return self._fetch_payloads(
                "SELECT payload FROM person_source_slices WHERE person_key=%s ORDER BY created_at",
                PersonSourceSlice, (person_key,),
            )
        return self._fetch_payloads("SELECT payload FROM person_source_slices ORDER BY created_at", PersonSourceSlice)

    def append_person_profile_revision(self, item: PersonProfileRevision) -> None:
        self._append_payload_record(
            "person_profile_revisions", "profile_revision_id", item.profile_revision_id, item,
            extra_columns=("person_key", "revision_number"),
            extra_values=(item.person_key, item.revision_number),
        )

    def list_person_profile_revisions(self, person_key: str | None = None) -> list[PersonProfileRevision]:
        if person_key:
            return self._fetch_payloads(
                "SELECT payload FROM person_profile_revisions WHERE person_key=%s ORDER BY revision_number,created_at",
                PersonProfileRevision, (person_key,),
            )
        return self._fetch_payloads("SELECT payload FROM person_profile_revisions ORDER BY created_at", PersonProfileRevision)

    def append_person_search_plan_revision(self, item: PersonSearchPlanRevision) -> None:
        self._append_payload_record(
            "person_search_plan_revisions", "search_plan_revision_id", item.search_plan_revision_id, item,
            extra_columns=("person_key", "revision_number"),
            extra_values=(item.person_key, item.revision_number),
        )

    def list_person_search_plan_revisions(self, person_key: str | None = None) -> list[PersonSearchPlanRevision]:
        if person_key:
            return self._fetch_payloads(
                "SELECT payload FROM person_search_plan_revisions WHERE person_key=%s ORDER BY revision_number,created_at",
                PersonSearchPlanRevision, (person_key,),
            )
        return self._fetch_payloads("SELECT payload FROM person_search_plan_revisions ORDER BY created_at", PersonSearchPlanRevision)

    def append_person_profile_import_run(self, item: PersonProfileImportRun) -> None:
        self._append_payload_record(
            "person_profile_import_runs", "import_run_id", item.import_run_id, item,
            extra_columns=("command_id",), extra_values=(item.command_id,),
        )

    def list_person_profile_import_runs(self) -> list[PersonProfileImportRun]:
        return self._fetch_payloads("SELECT payload FROM person_profile_import_runs ORDER BY created_at", PersonProfileImportRun)

    def append_person_profile_scan_run(self, item: PersonProfileScanRun) -> None:
        self._append_payload_record(
            "person_profile_scan_runs", "person_profile_scan_run_id", item.person_profile_scan_run_id, item,
            extra_columns=("command_id", "person_key"),
            extra_values=(item.command_id, item.person_key),
        )

    def list_person_profile_scan_runs(self, person_key: str | None = None) -> list[PersonProfileScanRun]:
        if person_key:
            return self._fetch_payloads(
                "SELECT payload FROM person_profile_scan_runs WHERE person_key=%s ORDER BY created_at",
                PersonProfileScanRun, (person_key,),
            )
        return self._fetch_payloads("SELECT payload FROM person_profile_scan_runs ORDER BY created_at", PersonProfileScanRun)

    def append_person_update_bundle(self, item: PersonUpdateBundle) -> None:
        self._append_payload_record(
            "person_update_bundles", "bundle_id", item.bundle_id, item,
            extra_columns=("person_key", "scan_run_id"),
            extra_values=(item.person_key, item.scan_run_id),
        )

    def list_person_update_bundles(self, person_key: str | None = None) -> list[PersonUpdateBundle]:
        if person_key:
            return self._fetch_payloads(
                "SELECT payload FROM person_update_bundles WHERE person_key=%s ORDER BY created_at",
                PersonUpdateBundle, (person_key,),
            )
        return self._fetch_payloads("SELECT payload FROM person_update_bundles ORDER BY created_at", PersonUpdateBundle)

    def append_person_digest_batch(self, item: PersonDigestBatch) -> None:
        self._append_payload_record("person_digest_batches", "digest_batch_id", item.digest_batch_id, item)

    def list_person_digest_batches(self) -> list[PersonDigestBatch]:
        return self._fetch_payloads("SELECT payload FROM person_digest_batches ORDER BY created_at", PersonDigestBatch)

    def append_profile_patch_review_batch(self, item: ProfilePatchReviewBatch) -> None:
        self._append_payload_record("profile_patch_review_batches", "review_batch_id", item.review_batch_id, item)

    def list_profile_patch_review_batches(self) -> list[ProfilePatchReviewBatch]:
        return self._fetch_payloads("SELECT payload FROM profile_patch_review_batches ORDER BY created_at", ProfilePatchReviewBatch)

    def append_profile_patch_review_item(self, item: ProfilePatchReviewItem) -> None:
        self._append_payload_record(
            "profile_patch_review_items", "review_item_id", item.review_item_id, item,
            extra_columns=("review_batch_id", "person_key"),
            extra_values=(item.review_batch_id, item.person_key),
        )

    def list_profile_patch_review_items(self, review_batch_id: str | None = None) -> list[ProfilePatchReviewItem]:
        if review_batch_id:
            return self._fetch_payloads(
                "SELECT payload FROM profile_patch_review_items WHERE review_batch_id=%s ORDER BY created_at",
                ProfilePatchReviewItem, (review_batch_id,),
            )
        return self._fetch_payloads("SELECT payload FROM profile_patch_review_items ORDER BY created_at", ProfilePatchReviewItem)

    def append_profile_patch_review_decision(self, item: ProfilePatchReviewDecision) -> None:
        self._append_payload_record(
            "profile_patch_review_decisions", "review_decision_id", item.review_decision_id, item,
            extra_columns=("command_id", "review_item_id"),
            extra_values=(item.command_id, item.review_item_id),
        )

    def list_profile_patch_review_decisions(self, review_item_id: str | None = None) -> list[ProfilePatchReviewDecision]:
        if review_item_id:
            return self._fetch_payloads(
                "SELECT payload FROM profile_patch_review_decisions WHERE review_item_id=%s ORDER BY created_at",
                ProfilePatchReviewDecision, (review_item_id,),
            )
        return self._fetch_payloads("SELECT payload FROM profile_patch_review_decisions ORDER BY created_at", ProfilePatchReviewDecision)

    def append_person_cognee_projection(self, item: PersonCogneeProjection) -> None:
        self._append_payload_record(
            "person_cognee_projections", "projection_id", item.projection_id, item,
            extra_columns=("person_key", "dataset_name"),
            extra_values=(item.person_key, item.dataset_name),
        )

    def list_person_cognee_projections(self, person_key: str | None = None) -> list[PersonCogneeProjection]:
        if person_key:
            return self._fetch_payloads(
                "SELECT payload FROM person_cognee_projections WHERE person_key=%s ORDER BY created_at",
                PersonCogneeProjection, (person_key,),
            )
        return self._fetch_payloads("SELECT payload FROM person_cognee_projections ORDER BY created_at", PersonCogneeProjection)

    def _fetch_payloads(self, sql: str, model: type[T], params: tuple[Any, ...] = ()) -> list[T]:
        """Validate models stored as a complete JSONB payload.

        Signals and candidates intentionally use envelope storage so new rule
        metadata can be added without a database migration. Their SELECT shape
        differs from normalized ledger tables and therefore must unwrap
        ``payload`` before Pydantic validation.
        """
        psycopg, dict_row = _psycopg()
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [model.model_validate(row["payload"]) for row in rows]
