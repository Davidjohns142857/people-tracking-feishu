from __future__ import annotations

from hashlib import sha256

from people_intel.ledger import Ledger
from people_intel.ontology import OntologyRegistry
from people_intel.schemas import Assertion, AssertionStatus, EpistemicType, Signal, SignalType


PREDICATE_SIGNAL_TYPES: dict[str, SignalType] = {
    "authored": SignalType.PUBLICATION,
    "published": SignalType.PUBLICATION,
    "maintains": SignalType.REPOSITORY_CREATED,
    "contributed_to": SignalType.RELEASE,
    "holds_role": SignalType.ROLE_CHANGE,
    "affiliated_with": SignalType.AFFILIATION_CHANGE,
    "founded": SignalType.STARTUP_LAUNCH,
    "cofounded": SignalType.STARTUP_LAUNCH,
    "raised_funding": SignalType.FUNDING,
    "launched": SignalType.PRODUCT_LAUNCH,
    "spun_out_from": SignalType.STARTUP_LAUNCH,
    "awarded": SignalType.AWARD,
}


class GraphSignalEngine:
    """Turns append-only graph changes into explainable, evidence-linked signals."""

    RULE_VERSION = "people-intel-signals-v1"

    def __init__(self, ledger: Ledger, ontology: OntologyRegistry):
        self.ledger = ledger
        self.ontology = ontology

    def evaluate_assertion(self, assertion_id: str) -> list[Signal]:
        assertion = self.ledger.get_assertion(assertion_id)
        signal_type = PREDICATE_SIGNAL_TYPES.get(assertion.predicate_id)
        if signal_type is None:
            return []

        alternatives = self._conflicting_assertions(assertion)
        strong_conflicts = [
            item
            for item in alternatives
            if item.status == AssertionStatus.HUMAN_CONFIRMED
            and assertion.epistemic_type != EpistemicType.HUMAN_JUDGMENT
        ]
        if strong_conflicts:
            signal_type = SignalType.DATA_QUALITY_CONFLICT

        base = 45.0 + assertion.confidence * 40.0 + min(10.0, len(assertion.evidence_span_ids) * 5.0)
        if alternatives:
            base += 5.0
        if strong_conflicts:
            base += 10.0
        object_id = assertion.object.entity_id or repr(assertion.object.value)
        graph_path = [
            assertion.subject_entity_id,
            assertion.predicate_id,
            object_id,
        ]
        conflict_ids = [item.assertion_id for item in alternatives]
        signal = Signal(
            signal_type=signal_type,
            title=self._title(signal_type, assertion),
            summary=self._summary(assertion, alternatives, strong_conflicts),
            score=min(100.0, base),
            confidence=assertion.confidence,
            entity_ids=[
                item
                for item in (assertion.subject_entity_id, assertion.object.entity_id)
                if item is not None
            ],
            triggering_assertion_ids=[assertion.assertion_id],
            evidence_span_ids=assertion.evidence_span_ids,
            alternative_assertion_ids=conflict_ids,
            time_boundary=assertion.valid_time,
            graph_path=graph_path,
            rule_id="new-temporal-graph-pattern",
            rule_version=self.RULE_VERSION,
            dedupe_key=self._dedupe_key(assertion, conflict_ids),
        )
        if not self._already_emitted(signal.dedupe_key):
            self.ledger.append_signal(signal)
            return [signal]
        return []

    def evaluate_all(self) -> list[Signal]:
        emitted: list[Signal] = []
        for assertion in self.ledger.list_assertions():
            emitted.extend(self.evaluate_assertion(assertion.assertion_id))
        return emitted

    def _conflicting_assertions(self, current: Assertion) -> list[Assertion]:
        predicate = self.ontology.get(current.predicate_id)
        if predicate.multi_valued and predicate.conflict_strategy == "coexist":
            return []
        current_key = self._object_key(current)
        return [
            item
            for item in self.ledger.list_assertions(current.subject_entity_id)
            if item.assertion_id != current.assertion_id
            and item.predicate_id == current.predicate_id
            and self._object_key(item) != current_key
            and self._intervals_may_overlap(current, item)
            and item.status not in {AssertionStatus.HUMAN_REJECTED, AssertionStatus.RETRACTED}
        ]

    def _already_emitted(self, dedupe_key: str | None) -> bool:
        return bool(dedupe_key) and any(item.dedupe_key == dedupe_key for item in self.ledger.list_signals())

    @staticmethod
    def _intervals_may_overlap(left: Assertion, right: Assertion) -> bool:
        left_start = left.valid_time.start.earliest if left.valid_time.start else None
        right_start = right.valid_time.start.earliest if right.valid_time.start else None
        left_end = left.valid_time.end.latest if left.valid_time.end else None
        right_end = right.valid_time.end.latest if right.valid_time.end else None
        if left_end and right_start and left_end < right_start:
            return False
        if right_end and left_start and right_end < left_start:
            return False
        return True

    @staticmethod
    def _object_key(assertion: Assertion) -> str:
        if assertion.object.entity_id:
            return f"entity:{assertion.object.entity_id}:{assertion.polarity}"
        return f"literal:{assertion.object.datatype}:{assertion.object.value!r}:{assertion.polarity}"

    @staticmethod
    def _dedupe_key(assertion: Assertion, alternatives: list[str]) -> str:
        material = "|".join([assertion.assertion_id, *sorted(alternatives)])
        return sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _title(signal_type: SignalType, assertion: Assertion) -> str:
        if signal_type == SignalType.DATA_QUALITY_CONFLICT:
            return f"Confirmed fact conflicts with new {assertion.predicate_id} assertion"
        return f"New {assertion.predicate_id} graph relation"

    @staticmethod
    def _summary(
        assertion: Assertion,
        alternatives: list[Assertion],
        strong_conflicts: list[Assertion],
    ) -> str:
        if strong_conflicts:
            return (
                f"Machine/reported assertion {assertion.assertion_id} conflicts with "
                f"human-confirmed assertions: {', '.join(item.assertion_id for item in strong_conflicts)}."
            )
        if alternatives:
            return (
                f"Assertion {assertion.assertion_id} adds a competing temporal interpretation; "
                f"alternatives remain visible: {', '.join(item.assertion_id for item in alternatives)}."
            )
        return f"Assertion {assertion.assertion_id} added a new evidence-backed graph relation."


__all__ = ["GraphSignalEngine", "PREDICATE_SIGNAL_TYPES"]
