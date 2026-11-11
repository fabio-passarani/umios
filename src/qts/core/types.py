"""Immutable domain types shared by strategies, risk, execution and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import uuid4


class TradingMode(StrEnum):
    """Operating mode of the platform. ``LIVE`` is reserved and disabled in the MVP."""

    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Side(StrEnum):
    """Order direction. The MVP is long-only spot: SELL only ever reduces a position."""

    BUY = "buy"
    SELL = "sell"


class OrderReason(StrEnum):
    """Why an order was generated (kept on fills and trades for attribution)."""

    ENTRY = "entry"
    EXIT_SIGNAL = "exit_signal"
    STOP_LOSS = "stop_loss"
    KILL_SWITCH = "kill_switch"


def _new_id() -> str:
    return uuid4().hex


@dataclass(frozen=True, slots=True)
class Order:
    """A request to trade. It is only a *request*: nothing happens until the
    independent risk engine approves it.

    ``reference_price`` is the price the order is expected to trade at (used for
    sizing and risk checks); ``stop_price`` is the protective stop attached to an
    entry and defines the capital at risk.
    """

    symbol: str
    side: Side
    quantity: float
    reference_price: float
    reason: OrderReason
    strategy: str
    created_at: datetime
    stop_price: float | None = None
    id: str = field(default_factory=_new_id)

    @property
    def notional(self) -> float:
        """Order value in quote currency at the reference price."""
        return self.quantity * self.reference_price


@dataclass(frozen=True, slots=True)
class Fill:
    """An executed order (simulated in backtest/paper)."""

    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float
    timestamp: datetime
    reason: OrderReason


@dataclass(frozen=True, slots=True)
class Trade:
    """A closed round trip (entry + exit) used for trade statistics."""

    symbol: str
    quantity: float
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    fees: float
    pnl: float
    return_pct: float
    exit_reason: OrderReason
