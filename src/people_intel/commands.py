from __future__ import annotations

import json
from typing import Callable, Protocol

from people_intel.ledger import Ledger
from people_intel.schemas import (
    AnnotationRequest,
    Assertion,
    AssertionObject,
    AssertionStatus,
    CommandEnvelope,
    CommandReceipt,
    EpistemicType,
    ObjectKind,
    SourceIngestionRequest,
    TransactionTime,
)
from people_intel.service import TemporalMemoryService


class CommandReceiptStore(Protocol):
    def get(self, command_id: str) -> CommandReceipt | None: ...
    def put(self, receipt: CommandReceipt) -> None: ...


class InMemoryCommandReceiptStore:
    def __init__(self):
        self._receipts: dict[str, CommandReceipt] = {}

    def get(self, command_id: str) -> CommandReceipt | None:
        return self._receipts.get(command_id)

    def put(self, receipt: CommandReceipt) -> None:
        if receipt.command_id in self._receipts:
            raise ValueError(f"command already processed: {receipt.command_id}")
        self._receipts[receipt.command_id] = receipt


class LedgerCommandReceiptStore:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def get(self, command_id: str) -> CommandReceipt | None:
        return self.ledger.get_command_receipt(command_id)

    def put(self, receipt: CommandReceipt) -> None:
        self.ledger.append_command_receipt(receipt)


class CommandProcessor:
    """Idempotent boundary shared by Feishu, API, Base and file imports."""

    def __init__(
        self,
        service: TemporalMemoryService,
        receipt_store: CommandReceiptStore | None = None,
        scan_request_handler: Callable[[CommandEnvelope], tuple[dict, str]] | None = None,
    ):
        self.service = service
        self.receipts = receipt_store or LedgerCommandReceiptStore(service.ledger)
        self.scan_request_handler = scan_request_handler

    def execute(self, command: CommandEnvelope) -> CommandReceipt:
        prior = self.receipts.get(command.command_id)
        if prior is not None:
            return prior
        handlers = {
            "source.ingest": self._ingest,
            "assertion.annotate": self._annotate,
            "identity.link": self._identity_link,
            "identity.split": self._identity_split,
            "scan.request": self._scan_request,
            "candidate.promote": self._candidate_promote,
        }
        result, status = handlers[command.command_type](command)
        receipt = CommandReceipt(
            command_id=command.command_id,
            command_type=command.command_type,
            status=status,
            result=result,
        )
        self.receipts.put(receipt)
        return receipt

    def _ingest(self, command: CommandEnvelope) -> tuple[dict, str]:
        payload = dict(command.payload)
        content = payload.get("content", "")
        if not isinstance(content, str):
            payload["content"] = json.dumps(content, ensure_ascii=False, sort_keys=True)
        payload.setdefault("retrieved_at", command.occurred_at)
        payload.setdefault("fetch_method", "feishu" if command.actor.channel.startswith("feishu") else "upload")
        payload.setdefault("rights_scope", "user_supplied")
        payload.pop("message_id", None)
        response = self.service.ingest_source(SourceIngestionRequest.model_validate(payload))
        return response.model_dump(mode="json"), "completed"

    def _annotate(self, command: CommandEnvelope) -> tuple[dict, str]:
        payload = dict(command.payload)
        payload.setdefault("actor_id", command.actor.actor_id)
        payload.setdefault("created_at", command.occurred_at)
        annotation = self.service.annotate(AnnotationRequest.model_validate(payload))
        return annotation.model_dump(mode="json"), "completed"

    def _identity_link(self, command: CommandEnvelope) -> tuple[dict, str]:
        assertion = self._identity_assertion(command, command.payload.get("predicate_id", "possibly_same_as"))
        self.service.create_assertion(assertion)
        return {"assertion_id": assertion.assertion_id}, "completed"

    def _identity_split(self, command: CommandEnvelope) -> tuple[dict, str]:
        assertion = self._identity_assertion(command, "not_same_as")
        self.service.create_assertion(assertion)
        return {"assertion_id": assertion.assertion_id}, "completed"

    def _identity_assertion(self, command: CommandEnvelope, predicate_id: str) -> Assertion:
        return Assertion(
            subject_entity_id=command.payload["left_entity_id"],
            predicate_id=predicate_id,
            object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=command.payload["right_entity_id"]),
            epistemic_type=EpistemicType.HUMAN_JUDGMENT,
            status=AssertionStatus.HUMAN_CONFIRMED,
            transaction_time=TransactionTime(
                observed_at=command.occurred_at,
                ingested_at=command.occurred_at,
            ),
            confidence=1.0,
            review_required=False,
            metadata={
                "command_id": command.command_id,
                "actor_id": command.actor.actor_id,
                "reason": command.payload.get("reason"),
            },
        )

    def _scan_request(self, command: CommandEnvelope) -> tuple[dict, str]:
        if self.scan_request_handler is not None:
            return self.scan_request_handler(command)
        return {
            "scan_request_id": f"scan_{command.command_id}",
            "entity_ids": command.payload.get("entity_ids", []),
            "scan_kind": command.payload.get("scan_kind", "incremental"),
        }, "accepted"

    @staticmethod
    def _candidate_promote(command: CommandEnvelope) -> tuple[dict, str]:
        # Promotion is emitted as an accepted orchestration request. Candidate
        # history remains append-only; the worker creates the tracking config.
        return {
            "promotion_request_id": f"promote_{command.command_id}",
            "candidate_id": command.payload["candidate_id"],
        }, "accepted"


__all__ = [
    "CommandProcessor",
    "CommandReceiptStore",
    "InMemoryCommandReceiptStore",
    "LedgerCommandReceiptStore",
]
