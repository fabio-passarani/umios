"""Brokers. The only way to make a broker act is to hand it a risk approval."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from qts.core.config import CostConfig
from qts.core.errors import ExecutionError, LiveTradingDisabledError
from qts.core.types import Fill, Side
from qts.execution.model import ExecutionModel, LegacyExecution, MarketConditions
from qts.portfolio.portfolio import Portfolio
from qts.risk.engine import RiskEngine
from qts.risk.models import RiskApproval


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """An order that the venue refused or only partly executed."""

    timestamp: datetime
    symbol: str
    side: Side
    status: str  # rejected | partial
    requested: float
    filled: float
    detail: str


class Broker(ABC):
    """Execution venue abstraction (extension point for IBKR, other exchanges…).

    The signature takes a :class:`RiskApproval`, not an ``Order``: there is no
    method that accepts an unapproved order.
    """

    @abstractmethod
    def execute(
        self,
        approval: RiskApproval,
        market_price: float,
        timestamp: datetime,
        forced: bool = False,
    ) -> Fill | None:
        """Execute an approved order at ``market_price`` (before frictions).
        ``None`` means the venue executed nothing."""


class PaperBroker(Broker):
    """Simulated spot broker used by backtests and paper trading.

    Asks the execution model what the order would have met in the market (see
    :mod:`qts.execution.model`), charges the fee and books the result on the
    simulated :class:`Portfolio`. No network, no credentials, no real money.

    ``execute`` returns ``None`` when the venue executed nothing (order refused,
    or no volume to trade against). The approval is consumed either way: an
    order that did not execute has to be evaluated again.
    """

    def __init__(
        self,
        risk_engine: RiskEngine,
        portfolio: Portfolio,
        costs: CostConfig,
        execution: ExecutionModel | None = None,
    ) -> None:
        self._risk = risk_engine
        self.portfolio = portfolio
        self.costs = costs
        self.execution = execution or LegacyExecution(costs)
        #: Market conditions of the candle being processed, per symbol (set by the session).
        self.market: dict[str, MarketConditions] = {}
        #: Orders the venue did not, or only partly, execute.
        self.events: list[ExecutionEvent] = []

    def execute(
        self,
        approval: RiskApproval,
        market_price: float,
        timestamp: datetime,
        forced: bool = False,
    ) -> Fill | None:
        # Raises RiskViolationError unless the approval is genuine, unused, current and
        # matches this execution (price for entries, time, kill switch).
        order = self._risk.consume(approval, market_price, timestamp)
        if market_price <= 0:
            raise ExecutionError(f"invalid market price {market_price} for {order.symbol}")
        outcome = self.execution.execute(
            order.side, order.quantity, market_price, self.market.get(order.symbol), forced
        )
        quantity, fee = outcome.quantity, outcome.fee
        if order.side is Side.BUY and quantity > 0:
            # never spend more than the account holds: shrink the fill, do not fail it
            cost = quantity * outcome.price + fee
            if cost > self.portfolio.cash:
                scale = max(self.portfolio.cash / cost * (1.0 - 1e-12), 0.0)
                quantity, fee = quantity * scale, fee * scale
        if outcome.status != "filled" or quantity < order.quantity:
            status = outcome.status if outcome.status != "filled" else "partial"
            detail = outcome.detail or "reduced to the available cash"
            self.events.append(
                ExecutionEvent(
                    timestamp, order.symbol, order.side, status, order.quantity, quantity, detail
                )
            )
        if quantity <= 0:
            return None
        share = quantity / outcome.quantity
        fill = Fill(
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=outcome.price,
            fee=fee,
            timestamp=timestamp,
            reason=order.reason,
            spread_cost=outcome.spread_cost * share,
            impact_cost=outcome.impact_cost * share,
            requested_quantity=order.quantity,
        )
        self.portfolio.apply_fill(fill, stop_price=order.stop_price)
        return fill


class LiveBroker(Broker):
    """Placeholder for real-money execution. **Not implemented in the MVP.**

    It cannot be instantiated: the class exists only to document the extension
    point and to make any accidental use fail immediately and loudly.
    """

    def __init__(self) -> None:
        raise LiveTradingDisabledError(
            "live trading is not implemented: no real orders can be sent by this version"
        )

    def execute(  # pragma: no cover - unreachable by construction
        self,
        approval: RiskApproval,
        market_price: float,
        timestamp: datetime,
        forced: bool = False,
    ) -> Fill | None:
        raise LiveTradingDisabledError("live trading is not implemented")
