from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from people_intel.ledger import Ledger


class GraphProjector(Protocol):
    def rebuild(self, ledger: Ledger) -> Any: ...


@dataclass(frozen=True)
class LedgerSnapshotManifest:
    schema_version: str
    counts: dict[str, int]
    collection_hashes: dict[str, str]
    root_hash: str


@dataclass(frozen=True)
class ProjectionRebuildReport:
    snapshot: LedgerSnapshotManifest
    graph_stats: dict[str, Any] | None


class ProjectionCoordinator:
    """Exports an audit snapshot and rebuilds disposable graph projections."""

    def __init__(self, ledger: Ledger, graph_projector: GraphProjector | None = None):
        self.ledger = ledger
        self.graph_projector = graph_projector

    def export_snapshot(self, output_dir: str | Path) -> LedgerSnapshotManifest:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        collections = self._collections()
        hashes: dict[str, str] = {}
        counts: dict[str, int] = {}
        for name, items in collections.items():
            path = output / f"{name}.jsonl"
            lines = [self._canonical_json(item) for item in items]
            payload = "".join(f"{line}\n" for line in lines)
            path.write_text(payload, encoding="utf-8")
            hashes[name] = sha256(payload.encode("utf-8")).hexdigest()
            counts[name] = len(items)
        root_material = "".join(f"{key}:{hashes[key]}\n" for key in sorted(hashes))
        manifest = LedgerSnapshotManifest(
            schema_version="people-intel-ledger-snapshot-v1",
            counts=counts,
            collection_hashes=hashes,
            root_hash=sha256(root_material.encode("utf-8")).hexdigest(),
        )
        (output / "manifest.json").write_text(
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest

    def rebuild(self, snapshot_dir: str | Path) -> ProjectionRebuildReport:
        manifest = self.export_snapshot(snapshot_dir)
        graph_stats = None
        if self.graph_projector is not None:
            result = self.graph_projector.rebuild(self.ledger)
            graph_stats = asdict(result) if hasattr(result, "__dataclass_fields__") else dict(result)
        return ProjectionRebuildReport(snapshot=manifest, graph_stats=graph_stats)

    def _collections(self) -> dict[str, list[Any]]:
        return {
            "source_document_versions": sorted(
                self.ledger.list_source_versions(), key=lambda item: item.source_version_id
            ),
            "episodes": sorted(self.ledger.list_episodes(), key=lambda item: item.episode_id),
            "evidence_spans": sorted(
                self.ledger.list_evidence_spans(), key=lambda item: item.evidence_span_id
            ),
            "entities": sorted(self.ledger.list_entities(), key=lambda item: item.entity_id),
            "extraction_runs": sorted(
                self.ledger.list_extraction_runs(), key=lambda item: item.extraction_run_id
            ),
            "initial_review_batches": sorted(
                self.ledger.list_initial_review_batches(), key=lambda item: item.review_batch_id
            ),
            "initial_review_items": sorted(
                self.ledger.list_initial_review_items(), key=lambda item: item.review_item_id
            ),
            "initial_review_decisions": sorted(
                self.ledger.list_initial_review_decisions(), key=lambda item: item.review_decision_id
            ),
            "assertions": sorted(
                self.ledger.list_assertions(), key=lambda item: item.assertion_id
            ),
            "assertion_relations": sorted(
                self.ledger.list_assertion_relations(), key=lambda item: item.assertion_relation_id
            ),
            "annotations": sorted(
                self.ledger.list_annotations(), key=lambda item: item.annotation_id
            ),
            "derived_assertion_inputs": sorted(
                self.ledger.list_derived_inputs(), key=lambda item: item.derived_assertion_id
            ),
            "signals": sorted(self.ledger.list_signals(), key=lambda item: item.signal_id),
            "candidates": sorted(
                self.ledger.list_candidates(), key=lambda item: item.candidate_id
            ),
        }

    @staticmethod
    def _canonical_json(item: Any) -> str:
        value = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "LedgerSnapshotManifest",
    "ProjectionCoordinator",
    "ProjectionRebuildReport",
]
