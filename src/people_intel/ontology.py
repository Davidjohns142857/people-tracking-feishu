from __future__ import annotations

import json
import os
from pathlib import Path

from people_intel.schemas import Assertion, Entity, ObjectKind, OntologyDefinition, Polarity, PredicateDefinition


class OntologyError(ValueError):
    pass


class OntologyRegistry:
    def __init__(self, definition: OntologyDefinition):
        self.definition = definition
        self._predicates = {item.predicate_id: item for item in definition.predicates}

    @classmethod
    def from_path(cls, path: str | Path) -> "OntologyRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        # Historical registries used the concise key ``id``. The public API
        # and ledger use ``predicate_id``; normalizing here keeps both ontology
        # encodings replayable without weakening the runtime schema.
        for predicate in raw.get("predicates", []):
            if "id" in predicate and "predicate_id" not in predicate:
                predicate["predicate_id"] = predicate.pop("id")
        return cls(OntologyDefinition.model_validate(raw))

    @classmethod
    def default(cls) -> "OntologyRegistry":
        project_root = Path(
            os.environ.get("PEOPLE_INTEL_PROJECT_ROOT", Path(__file__).resolve().parents[2])
        )
        path = project_root / "ontology" / "people-intel-v1.json"
        return cls.from_path(path)

    @property
    def version(self) -> str:
        return self.definition.version

    def get(self, predicate_id: str) -> PredicateDefinition:
        try:
            return self._predicates[predicate_id]
        except KeyError as exc:
            raise OntologyError(f"predicate is not registered: {predicate_id}") from exc

    def validate_assertion(self, assertion: Assertion, subject: Entity, object_entity: Entity | None) -> None:
        predicate = self.get(assertion.predicate_id)
        if assertion.ontology_version != self.version:
            raise OntologyError(
                f"assertion ontology version {assertion.ontology_version!r} does not match {self.version!r}"
            )
        if "*" not in predicate.domain and subject.entity_type not in predicate.domain:
            raise OntologyError(
                f"{assertion.predicate_id} does not allow subject type {subject.entity_type}"
            )
        if object_entity is not None and "*" not in predicate.range and object_entity.entity_type not in predicate.range:
            raise OntologyError(
                f"{assertion.predicate_id} does not allow object type {object_entity.entity_type}"
            )
        if assertion.object.kind == ObjectKind.LITERAL and "*" not in predicate.range and "Literal" not in predicate.range:
            raise OntologyError(f"{assertion.predicate_id} does not allow literal objects")
        if assertion.polarity == Polarity.NEGATIVE and not predicate.allow_negative:
            raise OntologyError(f"{assertion.predicate_id} does not allow negative assertions")


__all__ = ["OntologyError", "OntologyRegistry"]
