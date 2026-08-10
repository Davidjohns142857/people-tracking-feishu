from __future__ import annotations

import json
from dataclasses import dataclass

from people_intel.ledger import Ledger


def _driver(uri: str, user: str, password: str):
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("Neo4jProjector requires the 'neo4j' optional dependency") from exc
    return GraphDatabase.driver(uri, auth=(user, password))


@dataclass(frozen=True)
class ProjectionStats:
    entities: int = 0
    sources: int = 0
    spans: int = 0
    assertions: int = 0
    assertion_relations: int = 0


class Neo4jProjector:
    """Rebuildable Neo4j projection of the authoritative source/assertion ledger."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        self.uri = uri
        self.user = user
        self.password = password
        self.database = database

    def rebuild(self, ledger: Ledger) -> ProjectionStats:
        entities = ledger.list_entities()
        sources = ledger.list_source_versions()
        spans = ledger.list_evidence_spans()
        assertions = ledger.list_assertions()
        relations = ledger.list_assertion_relations()
        driver = _driver(self.uri, self.user, self.password)
        try:
            with driver.session(database=self.database) as session:
                session.run(
                    "MATCH (n) WHERE n:PeopleIntelEntity OR n:PeopleIntelAssertion "
                    "OR n:PeopleIntelEvidence OR n:PeopleIntelSource DETACH DELETE n"
                ).consume()
                session.run(
                    "CREATE CONSTRAINT people_intel_entity_id IF NOT EXISTS "
                    "FOR (n:PeopleIntelEntity) REQUIRE n.entity_id IS UNIQUE"
                ).consume()
                session.run(
                    "CREATE CONSTRAINT people_intel_assertion_id IF NOT EXISTS "
                    "FOR (n:PeopleIntelAssertion) REQUIRE n.assertion_id IS UNIQUE"
                ).consume()
                session.run(
                    "CREATE CONSTRAINT people_intel_span_id IF NOT EXISTS "
                    "FOR (n:PeopleIntelEvidence) REQUIRE n.evidence_span_id IS UNIQUE"
                ).consume()
                session.run(
                    "CREATE CONSTRAINT people_intel_source_id IF NOT EXISTS "
                    "FOR (n:PeopleIntelSource) REQUIRE n.source_version_id IS UNIQUE"
                ).consume()
                for entity in entities:
                    session.run(
                        "CREATE (n:PeopleIntelEntity {entity_id:$id, entity_type:$type, "
                        "canonical_name:$name, aliases_json:$aliases, metadata_json:$metadata})",
                        id=entity.entity_id,
                        type=str(entity.entity_type),
                        name=entity.canonical_name,
                        aliases=json.dumps(entity.aliases, ensure_ascii=False),
                        metadata=json.dumps(entity.metadata, ensure_ascii=False),
                    ).consume()
                for source in sources:
                    session.run(
                        "CREATE (n:PeopleIntelSource {source_version_id:$id, source_uri:$uri, "
                        "source_type:$type, content_hash:$hash, retrieved_at:$retrieved})",
                        id=source.source_version_id,
                        uri=source.source_uri,
                        type=str(source.source_type),
                        hash=source.content_hash,
                        retrieved=source.retrieved_at.isoformat(),
                    ).consume()
                for span in spans:
                    session.run(
                        "MATCH (s:PeopleIntelSource {source_version_id:$source_id}) "
                        "CREATE (e:PeopleIntelEvidence {evidence_span_id:$id, locator_type:$locator_type, "
                        "locator_json:$locator, quote:$quote, quote_hash:$quote_hash}) "
                        "CREATE (e)-[:IN_DOCUMENT]->(s)",
                        source_id=span.source_version_id,
                        id=span.evidence_span_id,
                        locator_type=span.locator_type,
                        locator=json.dumps(span.locator, ensure_ascii=False),
                        quote=span.quote,
                        quote_hash=span.quote_hash,
                    ).consume()
                for assertion in assertions:
                    params = {
                        "id": assertion.assertion_id,
                        "subject_id": assertion.subject_entity_id,
                        "predicate": assertion.predicate_id,
                        "polarity": str(assertion.polarity),
                        "epistemic": str(assertion.epistemic_type),
                        "status": str(assertion.status),
                        "confidence": assertion.confidence,
                        "valid_time": assertion.valid_time.model_dump_json(),
                        "transaction_time": assertion.transaction_time.model_dump_json(),
                        "object_json": assertion.object.model_dump_json(),
                    }
                    session.run(
                        "MATCH (subject:PeopleIntelEntity {entity_id:$subject_id}) "
                        "CREATE (a:PeopleIntelAssertion {assertion_id:$id, predicate_id:$predicate, "
                        "polarity:$polarity, epistemic_type:$epistemic, initial_status:$status, "
                        "confidence:$confidence, valid_time_json:$valid_time, "
                        "transaction_time_json:$transaction_time, object_json:$object_json}) "
                        "CREATE (subject)-[:SUBJECT_OF]->(a)",
                        **params,
                    ).consume()
                    if assertion.object.entity_id:
                        session.run(
                            "MATCH (a:PeopleIntelAssertion {assertion_id:$assertion_id}), "
                            "(object:PeopleIntelEntity {entity_id:$object_id}) "
                            "CREATE (a)-[:OBJECT_ENTITY]->(object)",
                            assertion_id=assertion.assertion_id,
                            object_id=assertion.object.entity_id,
                        ).consume()
                    for span_id in assertion.evidence_span_ids:
                        session.run(
                            "MATCH (a:PeopleIntelAssertion {assertion_id:$assertion_id}), "
                            "(e:PeopleIntelEvidence {evidence_span_id:$span_id}) "
                            "CREATE (a)-[:SUPPORTED_BY]->(e)",
                            assertion_id=assertion.assertion_id,
                            span_id=span_id,
                        ).consume()
                for relation in relations:
                    session.run(
                        "MATCH (a:PeopleIntelAssertion {assertion_id:$from_id}), "
                        "(b:PeopleIntelAssertion {assertion_id:$to_id}) "
                        "CREATE (a)-[:ASSERTION_RELATION {relation_type:$relation_type, "
                        "relation_id:$relation_id}]->(b)",
                        from_id=relation.from_assertion_id,
                        to_id=relation.to_assertion_id,
                        relation_type=str(relation.relation_type),
                        relation_id=relation.assertion_relation_id,
                    ).consume()
        finally:
            driver.close()
        return ProjectionStats(
            entities=len(entities),
            sources=len(sources),
            spans=len(spans),
            assertions=len(assertions),
            assertion_relations=len(relations),
        )
