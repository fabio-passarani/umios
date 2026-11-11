"""Brokers. The only way to make a broker act is to hand it a risk approval."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from qts.core.config import CostConfig
from qts.core.errors import ExecutionError, LiveTradingDisabledError
from qts.core.types import Fill, Side
from qts.portfolio.portfolio import Portfolio
from qts.risk.engine import RiskEngine
from qts.risk.models import RiskApproval


class Broker(ABC):
    """Execution venue abstraction (extension point for IBKR, other exchanges…).

    The signature takes a :class:`RiskApproval`, not an ``Order``: there is no
    method that accepts an unapproved order.
    """

    @abstractmethod
    def execute(self, approval: RiskApproval, market_price: float, timestamp: datetime) -> Fill:
        """Execute an approved order at ``market_price`` (before frictions)."""


class PaperBroker(Broker):
    """Simulated spot broker used by backtests and paper trading.

    Fills at the given market price worsened by slippage, charges the taker fee
    and books the result on the simulated :class:`Portfolio`. No network, no
    credentials, no real money.
    """

    def __init__(self, risk_engine: RiskEngine, portfolio: Portfolio, costs: CostConfig) -> None:
        self._risk = risk_engine
        self.portfolio = portfolio
        self.costs = costs

    def execute(self, approval: RiskApproval, market_price: float, timestamp: datetime) -> Fill:
        # Raises RiskViolationError unless the approval is genuine, unused, current and
        # matches this execution (price for entries, time, kill switch).
        order = self._risk.consume(approval, market_price, timestamp)
        if market_price <= 0:
            raise ExecutionError(f"invalid market price {market_price} for {order.symbol}")
        slip = self.costs.slippage_rate
        price = market_price * (1.0 + slip if order.side is Side.BUY else 1.0 - slip)
        fee = order.quantity * price * self.costs.fee_rate
        fill = Fill(
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            fee=fee,
            timestamp=timestamp,
            reason=order.reason,
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
        self, approval: RiskApproval, market_price: float, timestamp: datetime
    ) -> Fill:
        raise LiveTradingDisabledError("live trading is not implemented")
