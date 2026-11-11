"""Append-only record of every risk decision and risk event."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from qts.core.logging import get_logger
from qts.risk.models import RiskDecision

log = get_logger("risk.journal")


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """A state change of the engine (halt, kill switch, error) outside an order."""

    timestamp: datetime
    kind: str
    detail: str


class RiskJournal:
    """Keeps decisions in memory, logs them, and forwards them to optional sinks
    (e.g. the database writer) so that nothing the engine decides goes unrecorded."""

    def __init__(self, sinks: list[Callable[[RiskDecision], None]] | None = None) -> None:
        self.decisions: list[RiskDecision] = []
        self.events: list[RiskEvent] = []
        self._sinks = sinks or []

    def record(self, decision: RiskDecision) -> None:
        self.decisions.append(decision)
        order = decision.order
        emit = log.debug if decision.approved else log.warning
        emit(
            "risk_decision",
            approved=decision.approved,
            order_id=order.id,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            order_reason=order.reason.value,
            rejected_by=[c.name for c in decision.failed_checks],
            detail=decision.reason,
            at=decision.timestamp.isoformat(),
        )
        for sink in self._sinks:
            sink(decision)

    def record_event(self, timestamp: datetime, kind: str, detail: str) -> None:
        self.events.append(RiskEvent(timestamp, kind, detail))
        log.warning("risk_event", kind=kind, detail=detail, at=timestamp.isoformat())
