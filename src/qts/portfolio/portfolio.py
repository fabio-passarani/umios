"""Cash and position accounting for a long-only spot account."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from qts.core.errors import ExecutionError
from qts.core.types import Fill, Side, Trade

_EPSILON = 1e-12


@dataclass(slots=True)
class Position:
    """An open long position."""

    symbol: str
    quantity: float
    avg_price: float
    entry_time: datetime
    entry_fees: float
    stop_price: float | None = None
    highest_close: float | None = None

    def market_value(self, price: float) -> float:
        return self.quantity * price


class Portfolio:
    """Tracks cash, open positions and closed trades.

    It is a passive ledger: it applies fills it is given and refuses impossible
    ones (overspending cash, selling more than held). Deciding *whether* a trade
    should happen is the risk engine's job, not the ledger's.
    """

    def __init__(self, initial_capital: float) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.fills: list[Fill] = []

    def exposure(self, prices: dict[str, float]) -> float:
        """Market value of all open positions."""
        return sum(p.market_value(prices[s]) for s, p in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        """Cash plus market value of open positions."""
        return self.cash + self.exposure(prices)

    def apply_fill(self, fill: Fill, stop_price: float | None = None) -> Trade | None:
        """Apply an execution. Returns the closed :class:`Trade` for sells."""
        if fill.quantity <= 0 or fill.price <= 0 or fill.fee < 0:
            raise ExecutionError(f"invalid fill: {fill}")
        notional = fill.quantity * fill.price
        if fill.side is Side.BUY:
            cost = notional + fill.fee
            if cost > self.cash + 1e-9:
                raise ExecutionError(
                    f"insufficient cash for {fill.symbol}: need {cost:.2f}, have {self.cash:.2f}"
                )
            self.cash -= cost
            current = self.positions.get(fill.symbol)
            if current is None:
                self.positions[fill.symbol] = Position(
                    symbol=fill.symbol,
                    quantity=fill.quantity,
                    avg_price=fill.price,
                    entry_time=fill.timestamp,
                    entry_fees=fill.fee,
                    stop_price=stop_price,
                    highest_close=fill.price,
                )
            else:
                total = current.quantity + fill.quantity
                current.avg_price = (
                    current.avg_price * current.quantity + fill.price * fill.quantity
                ) / total
                current.quantity = total
                current.entry_fees += fill.fee
                if stop_price is not None:
                    current.stop_price = stop_price
            self.fills.append(fill)
            return None

        position = self.positions.get(fill.symbol)
        if position is None or fill.quantity > position.quantity + _EPSILON:
            raise ExecutionError(f"cannot sell {fill.quantity} {fill.symbol}: not held (no shorts)")
        share = min(fill.quantity / position.quantity, 1.0)
        entry_fees = position.entry_fees * share
        self.cash += notional - fill.fee
        fees = entry_fees + fill.fee
        pnl = (fill.price - position.avg_price) * fill.quantity - fees
        trade = Trade(
            symbol=fill.symbol,
            quantity=fill.quantity,
            entry_time=position.entry_time,
            exit_time=fill.timestamp,
            entry_price=position.avg_price,
            exit_price=fill.price,
            fees=fees,
            pnl=pnl,
            return_pct=pnl / (position.avg_price * fill.quantity),
            exit_reason=fill.reason,
        )
        position.quantity -= fill.quantity
        position.entry_fees -= entry_fees
        if position.quantity <= _EPSILON:
            del self.positions[fill.symbol]
        self.trades.append(trade)
        self.fills.append(fill)
        return trade
