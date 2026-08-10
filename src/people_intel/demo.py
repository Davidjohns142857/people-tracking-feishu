from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from people_intel.events import EventBroker
from people_intel.importers import IcmlPeopleMarkdownImporter
from people_intel.projections import ProjectionCoordinator
from people_intel.schemas import (
    AnnotationAction,
    AnnotationRequest,
    Assertion,
    AssertionObject,
    AssertionStatus,
    DemoScenario,
    DemoScenarioStep,
    DemoSession,
    DeriveRequest,
    Entity,
    EntityType,
    EpistemicType,
    FetchMethod,
    GraphEdge,
    GraphNode,
    GraphQuery,
    IntervalType,
    MemoryGraph,
    ObjectKind,
    ScenarioStepResult,
    SourceIngestionRequest,
    SourceType,
    TemporalExtent,
    TemporalPoint,
    TemporalPrecision,
    TransactionTime,
    utc_now,
)
from people_intel.service import TemporalMemoryService


UTC = timezone.utc
BASE_TIME = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


SCENARIO = DemoScenario(
    scenario_id="complete-mvp",
    name="Temporal memory: complete MVP walkthrough",
    description="From immutable source versions to explainable working views, identity and signals.",
    steps=[
        DemoScenarioStep(step_id="source-v1", position=1, title="Ingest source v1", description="Store an immutable profile version by SHA-256.", endpoint="POST /v1/source-documents"),
        DemoScenarioStep(step_id="source-dedup", position=2, title="Prove idempotency", description="Repeat the same URI/hash and receive unchanged.", endpoint="POST /v1/source-documents"),
        DemoScenarioStep(step_id="source-v2", position=3, title="Append source v2", description="Changed content creates a linked version, never an overwrite.", endpoint="POST /v1/source-documents"),
        DemoScenarioStep(step_id="assertion", position=4, title="Extract evidence and assertion", description="Create a month-precision role relation anchored to a source span.", endpoint="POST /v1/demo/scenarios/complete-mvp/run"),
        DemoScenarioStep(step_id="time-machine", position=5, title="Travel across known time", description="Compare what was known before and after ingestion.", endpoint="POST /v1/graph/query"),
        DemoScenarioStep(step_id="conflict", position=6, title="Keep conflicting claims", description="A second employer claim coexists with the first.", endpoint="POST /v1/demo/scenarios/complete-mvp/run"),
        DemoScenarioStep(step_id="annotation", position=7, title="Govern without mutation", description="Confirm and correct by adding annotations and a replacement assertion.", endpoint="POST /v1/annotations"),
        DemoScenarioStep(step_id="derived-stale", position=8, title="Invalidate derived memory", description="Rejecting a dependency makes its derived assertion stale.", endpoint="POST /v1/assertions/{id}/derive"),
        DemoScenarioStep(step_id="identity", position=9, title="Link and split identity", description="Identity relations remain reversible without recycling entity IDs.", endpoint="POST /v1/commands"),
        DemoScenarioStep(step_id="signal-snapshot", position=10, title="Emit signal and snapshot", description="Create an evidence-linked graph signal and deterministic audit snapshot.", endpoint="GET /v1/signals"),
    ],
)


class DemoController:
    def __init__(
        self,
        service: TemporalMemoryService,
        broker: EventBroker,
        project_root: Path,
    ):
        self.service = service
        self.broker = broker
        self.project_root = project_root
        self.session: DemoSession | None = None
        self.results: dict[int, ScenarioStepResult] = {}
        self.state: dict[str, Any] = {}

    def create_session(self) -> DemoSession:
        if self.session is not None:
            return self.session
        fixture = self.project_root / "fixtures" / "icml_2026_people_excerpt.md"
        report = IcmlPeopleMarkdownImporter(self.service).import_path(
            fixture,
            observed_at=BASE_TIME - timedelta(days=1),
        )
        self.session = DemoSession(session_id=f"demo_{uuid4().hex}")
        self.state["icml_source_id"] = report.source_version_id
        self.broker.publish(
            "demo.session.created",
            "demo_session",
            self.session.session_id,
            payload={"people": len(report.people), "papers": len(report.paper_entity_ids)},
        )
        return self.session

    def run(self, requested_step: int | None = None) -> ScenarioStepResult:
        session = self.create_session()
        step = requested_step or session.current_step + 1
        if step in self.results:
            previous = self.results[step]
            return previous.model_copy(update={"status": "already_completed"})
        if step != session.current_step + 1:
            raise ValueError(f"demo steps are sequential; next step is {session.current_step + 1}")
        handlers: dict[int, Callable[[], tuple[dict[str, Any], dict[str, Any], list[str]]]] = {
            1: self._source_v1,
            2: self._source_dedup,
            3: self._source_v2,
            4: self._assertion,
            5: self._time_machine,
            6: self._conflict,
            7: self._annotation,
            8: self._derived_stale,
            9: self._identity,
            10: self._signal_snapshot,
        }
        meta = SCENARIO.steps[step - 1]
        self.session = session.model_copy(update={"status": "running", "updated_at": utc_now()})
        request, response, changed = handlers[step]()
        event = self.broker.publish(
            "demo.step.completed",
            "demo_step",
            meta.step_id,
            trace_id=session.session_id,
            payload={"position": step, "changed_ids": changed},
        )
        result = ScenarioStepResult(
            session_id=session.session_id,
            scenario_id=SCENARIO.scenario_id,
            step_id=meta.step_id,
            position=step,
            title=meta.title,
            status="completed",
            request=request,
            response=response,
            changed_ids=changed,
            event_id=event.event_id,
        )
        self.results[step] = result
        updates = {
            "current_step": step,
            "status": "completed" if step == len(SCENARIO.steps) else "ready",
            "focus_entity_id": self.state.get("person_id"),
            "updated_at": utc_now(),
        }
        self.session = self.session.model_copy(update=updates)
        return result

    def _source_request(self, content: str, minute: int) -> SourceIngestionRequest:
        return SourceIngestionRequest(
            source_uri="https://demo.people-intel.local/alice",
            source_type=SourceType.HOMEPAGE,
            media_type="text/plain",
            content=content,
            normalized_markdown=content,
            retrieved_at=BASE_TIME + timedelta(minutes=minute),
            source_identity="Alice official profile",
            fetch_method=FetchMethod.QIAOMU,
        )

    def _source_v1(self):
        request = self._source_request("Alice is a researcher at Example AI.", 1)
        response = self.service.ingest_source(request)
        self.state["source_v1"] = response.source_version_id
        return request.model_dump(mode="json"), response.model_dump(mode="json"), [response.source_version_id]

    def _source_dedup(self):
        request = self._source_request("Alice is a researcher at Example AI.", 2)
        response = self.service.ingest_source(request)
        return request.model_dump(mode="json"), response.model_dump(mode="json"), []

    def _source_v2(self):
        content = "Alice joined Example AI in May 2026 and now leads applied research."
        request = self._source_request(content, 3)
        response = self.service.ingest_source(request)
        self.state["source_v2"] = response.source_version_id
        return request.model_dump(mode="json"), response.model_dump(mode="json"), [response.source_version_id]

    def _assertion(self):
        person = self.service.create_entity(Entity(entity_type=EntityType.PERSON, canonical_name="Alice Demo"))
        company = self.service.create_entity(Entity(entity_type=EntityType.COMPANY, canonical_name="Example AI"))
        quote = "Alice joined Example AI in May 2026"
        span = self.service.create_span(
            self.state["source_v2"], locator_type="character", locator={"start": 0, "end": len(quote)}, quote=quote
        )
        known = BASE_TIME + timedelta(minutes=4)
        assertion = self.service.create_assertion(
            Assertion(
                subject_entity_id=person.entity_id,
                predicate_id="worked_at",
                object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=company.entity_id),
                valid_time=TemporalExtent(
                    start=TemporalPoint(
                        earliest=datetime(2026, 5, 1, tzinfo=UTC),
                        latest=datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC),
                        precision=TemporalPrecision.MONTH,
                    ),
                    interval_type=IntervalType.OPEN_END,
                ),
                transaction_time=TransactionTime(observed_at=known, ingested_at=known),
                evidence_span_ids=[span.evidence_span_id],
                confidence=0.91,
            )
        )
        self.state.update(person_id=person.entity_id, company_a_id=company.entity_id, span_id=span.evidence_span_id, assertion_a_id=assertion.assertion_id)
        response = {"person": person, "company": company, "span": span, "assertion": assertion}
        return {"source_version_id": self.state["source_v2"], "quote": quote}, _dump(response), [person.entity_id, company.entity_id, span.evidence_span_id, assertion.assertion_id]

    def _time_machine(self):
        person_id = self.state["person_id"]
        valid_at = datetime(2026, 5, 15, tzinfo=UTC)
        before = self.service.working_view(GraphQuery(subject_entity_id=person_id, valid_at=valid_at, known_at=BASE_TIME + timedelta(minutes=3, seconds=59)))
        after = self.service.working_view(GraphQuery(subject_entity_id=person_id, valid_at=valid_at, known_at=BASE_TIME + timedelta(minutes=5)))
        return {"entity_id": person_id, "valid_at": valid_at.isoformat()}, {"before": before.model_dump(mode="json"), "after": after.model_dump(mode="json")}, []

    def _conflict(self):
        company = self.service.create_entity(Entity(entity_type=EntityType.COMPANY, canonical_name="New Venture Labs"))
        known = BASE_TIME + timedelta(minutes=6)
        first = self.service.ledger.get_assertion(self.state["assertion_a_id"])
        assertion = self.service.create_assertion(
            first.model_copy(update={
                "assertion_id": f"ast_{uuid4().hex}",
                "object": AssertionObject(kind=ObjectKind.ENTITY, entity_id=company.entity_id),
                "transaction_time": TransactionTime(observed_at=known, ingested_at=known),
                "confidence": 0.84,
                "metadata": {"demo": "conflicting employer claim"},
            })
        )
        self.state.update(company_b_id=company.entity_id, assertion_b_id=assertion.assertion_id)
        return {"subject": self.state["person_id"], "predicate": "worked_at"}, _dump({"company": company, "assertion": assertion}), [company.entity_id, assertion.assertion_id]

    def _annotation(self):
        confirm = self.service.annotate(AnnotationRequest(
            target_assertion_id=self.state["assertion_a_id"], action=AnnotationAction.CONFIRM,
            reason="Reviewed against the official demo profile.", actor_id="demo_reviewer",
            created_at=BASE_TIME + timedelta(minutes=7),
        ))
        old = self.service.ledger.get_assertion(self.state["assertion_b_id"])
        replacement = old.model_copy(update={
            "assertion_id": f"ast_{uuid4().hex}",
            "predicate_id": "affiliated_with",
            "transaction_time": TransactionTime(observed_at=BASE_TIME + timedelta(minutes=7), ingested_at=BASE_TIME + timedelta(minutes=7)),
        })
        correction = self.service.annotate(AnnotationRequest(
            target_assertion_id=old.assertion_id, action=AnnotationAction.CORRECT,
            replacement_assertion=replacement, reason="The source supports an affiliation, not employment.",
            actor_id="demo_reviewer", created_at=BASE_TIME + timedelta(minutes=7),
        ))
        self.state["replacement_id"] = correction.replacement_assertion_id
        return {"confirm": self.state["assertion_a_id"], "correct": old.assertion_id}, _dump({"confirmation": confirm, "correction": correction, "replacement": self.service.ledger.get_assertion(correction.replacement_assertion_id or "")}), [confirm.annotation_id, correction.annotation_id, correction.replacement_assertion_id or ""]

    def _derived_stale(self):
        topic = self.service.create_entity(Entity(entity_type=EntityType.TOPIC, canonical_name="Applied AI startups"))
        dependency = self.service.ledger.get_assertion(self.state["replacement_id"])
        known = BASE_TIME + timedelta(minutes=8)
        derived = self.service.derive(DeriveRequest(
            assertion=Assertion(
                subject_entity_id=self.state["person_id"], predicate_id="interested_in",
                object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=topic.entity_id),
                epistemic_type=EpistemicType.DERIVED,
                transaction_time=TransactionTime(observed_at=known, ingested_at=known),
                evidence_span_ids=dependency.evidence_span_ids, confidence=0.82,
            ),
            input_assertion_ids=[dependency.assertion_id], rule_id="affiliation-interest-hypothesis", rule_version="1",
            model_name="demo-rule-engine", prompt_hash="demo",
        ))
        rejection = self.service.annotate(AnnotationRequest(
            target_assertion_id=dependency.assertion_id, action=AnnotationAction.REJECT,
            reason="Demo rejection invalidates downstream inference.", actor_id="demo_reviewer",
            created_at=BASE_TIME + timedelta(minutes=8, seconds=1),
        ))
        effective = self.service.effective_status(derived, BASE_TIME + timedelta(minutes=9))
        self.state["derived_id"] = derived.assertion_id
        return {"dependency": dependency.assertion_id, "rule": "affiliation-interest-hypothesis"}, _dump({"derived": derived, "rejection": rejection, "derived_effective_status": effective}), [topic.entity_id, derived.assertion_id, rejection.annotation_id]

    def _identity(self):
        person = self.service.create_entity(Entity(entity_type=EntityType.IDENTITY_ACCOUNT, canonical_name="github.com/alice-demo"))
        other = self.service.create_entity(Entity(entity_type=EntityType.IDENTITY_ACCOUNT, canonical_name="x.com/alice-demo"))
        known = BASE_TIME + timedelta(minutes=9)
        linked = self.service.create_assertion(Assertion(
            subject_entity_id=person.entity_id, predicate_id="possibly_same_as",
            object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=other.entity_id),
            epistemic_type=EpistemicType.HUMAN_JUDGMENT, status=AssertionStatus.HUMAN_CONFIRMED,
            transaction_time=TransactionTime(observed_at=known, ingested_at=known), confidence=1.0,
        ))
        split = self.service.create_assertion(Assertion(
            subject_entity_id=person.entity_id, predicate_id="not_same_as",
            object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=other.entity_id),
            epistemic_type=EpistemicType.HUMAN_JUDGMENT, status=AssertionStatus.HUMAN_CONFIRMED,
            transaction_time=TransactionTime(observed_at=known + timedelta(seconds=1), ingested_at=known + timedelta(seconds=1)), confidence=1.0,
        ))
        cluster = self.service.identity_cluster(person.entity_id)
        return {"left": person.entity_id, "right": other.entity_id}, _dump({"link": linked, "split": split, "cluster_after_split": cluster}), [person.entity_id, other.entity_id, linked.assertion_id, split.assertion_id]

    def _signal_snapshot(self):
        role = self.service.create_entity(Entity(entity_type=EntityType.ROLE, canonical_name="Founder"))
        known = BASE_TIME + timedelta(minutes=10)
        role_assertion = self.service.create_assertion(Assertion(
            subject_entity_id=self.state["person_id"], predicate_id="holds_role",
            object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=role.entity_id),
            transaction_time=TransactionTime(observed_at=known, ingested_at=known),
            evidence_span_ids=[self.state["span_id"]], confidence=0.88,
        ))
        signal = self.service.create_signal_for_assertion(role_assertion.assertion_id)
        output = self.project_root / ".people_intel" / "demo-snapshots" / (self.session.session_id if self.session else "latest")
        manifest = ProjectionCoordinator(self.service.ledger).export_snapshot(output)
        return {"assertion_id": role_assertion.assertion_id}, _dump({"signal": signal, "snapshot": manifest}), [role.entity_id, role_assertion.assertion_id, signal.signal_id if signal else ""]

    def graph(self) -> MemoryGraph:
        return build_memory_graph(self.service)


def build_memory_graph(service: TemporalMemoryService) -> MemoryGraph:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        for entity in service.ledger.list_entities():
            nodes.append(GraphNode(id=entity.entity_id, node_type="entity", label=entity.canonical_name, subtype=str(entity.entity_type), data=entity.model_dump(mode="json")))
        for source in service.ledger.list_source_versions():
            nodes.append(GraphNode(id=source.source_version_id, node_type="source", label=source.source_uri.rsplit("/", 1)[-1] or source.source_type, subtype=str(source.source_type), data=source.model_dump(mode="json")))
        for span in service.ledger.list_evidence_spans():
            nodes.append(GraphNode(id=span.evidence_span_id, node_type="evidence", label=span.quote[:48], subtype=span.locator_type, data=span.model_dump(mode="json")))
            edges.append(GraphEdge(id=f"edge_{span.evidence_span_id}_source", source=span.evidence_span_id, target=span.source_version_id, label="IN_DOCUMENT"))
        for assertion in service.ledger.list_assertions():
            nodes.append(GraphNode(id=assertion.assertion_id, node_type="assertion", label=assertion.predicate_id, subtype=str(service.effective_status(assertion, utc_now())), data=assertion.model_dump(mode="json")))
            edges.append(GraphEdge(id=f"edge_{assertion.subject_entity_id}_{assertion.assertion_id}", source=assertion.subject_entity_id, target=assertion.assertion_id, label="SUBJECT_OF"))
            if assertion.object.entity_id:
                edges.append(GraphEdge(id=f"edge_{assertion.assertion_id}_{assertion.object.entity_id}", source=assertion.assertion_id, target=assertion.object.entity_id, label="OBJECT"))
            else:
                literal_id = f"lit_{assertion.assertion_id}"
                nodes.append(GraphNode(id=literal_id, node_type="literal", label=str(assertion.object.value), subtype=str(assertion.object.datatype), data=assertion.object.model_dump(mode="json")))
                edges.append(GraphEdge(id=f"edge_{assertion.assertion_id}_{literal_id}", source=assertion.assertion_id, target=literal_id, label="OBJECT"))
            for span_id in assertion.evidence_span_ids:
                edges.append(GraphEdge(id=f"edge_{assertion.assertion_id}_{span_id}", source=assertion.assertion_id, target=span_id, label="SUPPORTED_BY"))
        for relation in service.ledger.list_assertion_relations():
            edges.append(GraphEdge(id=relation.assertion_relation_id, source=relation.from_assertion_id, target=relation.to_assertion_id, label=str(relation.relation_type)))
        return MemoryGraph(nodes=nodes, edges=edges)


def _dump(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return _dump(asdict(value))
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return value


__all__ = ["DemoController", "SCENARIO", "build_memory_graph"]
