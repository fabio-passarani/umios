"""The single path from an order to a broker."""

from __future__ import annotations

from datetime import datetime

from qts.core.types import Fill, Order
from qts.execution.broker import Broker
from qts.risk.engine import RiskContext, RiskEngine
from qts.risk.models import RiskDecision


class OrderRouter:
    """Asks the risk engine first; forwards to the broker only on approval."""

    def __init__(self, risk_engine: RiskEngine, broker: Broker) -> None:
        self._risk = risk_engine
        self._broker = broker

    def submit(
        self, order: Order, context: RiskContext, market_price: float, timestamp: datetime
    ) -> tuple[RiskDecision, Fill | None]:
        """Evaluate and, if approved, execute. Rejected orders return ``(decision, None)``."""
        decision = self._risk.evaluate(order, context)
        if not decision.approved or decision.approval is None:
            return decision, None
        return decision, self._broker.execute(decision.approval, market_price, timestamp)
