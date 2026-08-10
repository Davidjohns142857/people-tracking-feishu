from __future__ import annotations

from collections import deque
from threading import Lock
from typing import Any

from people_intel.schemas import DomainEvent


class EventBroker:
    """Best-effort in-process event stream; the ledger remains authoritative."""

    def __init__(self, max_events: int = 512):
        self._events: deque[DomainEvent] = deque(maxlen=max_events)
        self._next_id = 1
        self._lock = Lock()

    def publish(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str | None = None,
        *,
        trace_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> DomainEvent:
        with self._lock:
            event = DomainEvent(
                event_id=self._next_id,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                trace_id=trace_id,
                payload=payload or {},
            )
            self._next_id += 1
            self._events.append(event)
            return event

    def since(self, event_id: int = 0) -> list[DomainEvent]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._events if item.event_id > event_id]

    @property
    def latest_id(self) -> int:
        with self._lock:
            return self._events[-1].event_id if self._events else 0


__all__ = ["EventBroker"]
