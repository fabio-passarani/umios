"""Bar-by-bar trading session shared by backtesting and paper trading.

Using one code path for both modes is deliberate: the paper account then behaves
exactly like the backtest it was validated with, and any divergence between the
two can only come from data, never from logic.

Per bar *t* the order of events is:

1. execute decisions taken at the close of *t-1* at the **open** of *t*
   (exits first, to free cash);
2. check protective stops against the low of *t*;
3. mark to market at the close of *t* and inform the risk engine;
4. read the strategy's signals for *t* (data up to the close of *t* only) and
   queue them for the next open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qts.core.config import CostConfig
from qts.core.types import Fill, Order, OrderReason, Side, Trade
from qts.execution.broker import PaperBroker
from qts.execution.router import OrderRouter
from qts.portfolio.portfolio import Portfolio, Position
from qts.portfolio.sizing import PositionSizer
from qts.risk.engine import RiskContext, RiskEngine


@dataclass(frozen=True, slots=True)
class Bar:
    """One closed candle."""

    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class SignalRow:
    """The strategy's output for one symbol at one bar."""

    entry: bool
    exit: bool
    stop_distance: float
    trail_distance: float


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    cash: float
    exposure: float


@dataclass(slots=True)
class _Pending:
    kind: str  # "entry" | "exit"
    stop_distance: float = math.nan
    reason: OrderReason = OrderReason.EXIT_SIGNAL


class TradingSession:
    """Owns the simulated account and advances it one bar at a time."""

    def __init__(
        self,
        strategy_name: str,
        sizer: PositionSizer,
        risk_engine: RiskEngine,
        costs: CostConfig,
        initial_capital: float,
        bar_duration: Any,
    ) -> None:
        self.strategy_name = strategy_name
        self.sizer = sizer
        self.risk = risk_engine
        self.costs = costs
        self.portfolio = Portfolio(initial_capital)
        self.broker = PaperBroker(risk_engine, self.portfolio, costs)
        self.router = OrderRouter(risk_engine, self.broker)
        self.bar_duration = bar_duration
        self.equity_curve: list[EquityPoint] = []
        self.last_close: dict[str, float] = {}
        self.last_bar_time: datetime | None = None
        self._pending: dict[str, _Pending] = {}

    # ----------------------------------------------------------------- helpers
    def _context(self, now: datetime) -> RiskContext:
        data_time = (
            self.last_bar_time + self.bar_duration if self.last_bar_time is not None else now
        )
        return RiskContext(
            now=now,
            cash=self.portfolio.cash,
            positions={s: p.quantity for s, p in self.portfolio.positions.items()},
            prices=dict(self.last_close),
            data_time=data_time,
            cost_rate=self.costs.cost_rate,
        )

    def _sell(
        self, position: Position, price: float, reason: OrderReason, now: datetime
    ) -> Fill | None:
        order = Order(
            symbol=position.symbol,
            side=Side.SELL,
            quantity=position.quantity,
            reference_price=price,
            reason=reason,
            strategy=self.strategy_name,
            created_at=now,
        )
        _decision, fill = self.router.submit(order, self._context(now), price, now)
        return fill

    def _enter(self, symbol: str, open_price: float, stop_distance: float, now: datetime) -> None:
        if symbol in self.portfolio.positions:
            return
        context = self._context(now)
        quantity = self.sizer.size(
            equity=context.equity,
            cash=context.cash,
            price=open_price,
            stop_distance=stop_distance,
            cost_rate=self.costs.cost_rate,
        )
        stop_price = open_price - stop_distance
        if quantity <= 0 or stop_price <= 0:
            return
        order = Order(
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            reference_price=open_price,
            reason=OrderReason.ENTRY,
            strategy=self.strategy_name,
            created_at=now,
            stop_price=stop_price,
        )
        self.router.submit(order, context, open_price, now)

    # -------------------------------------------------------------------- core
    def process_bar(
        self, timestamp: datetime, bars: dict[str, Bar], signals: dict[str, SignalRow]
    ) -> EquityPoint:
        """Advance the account by one bar. ``bars`` holds the symbols that traded."""
        symbols = sorted(bars)

        # 1) decisions from the previous close, executed at this open (exits first)
        for symbol in symbols:
            pending = self._pending.get(symbol)
            position = self.portfolio.positions.get(symbol)
            if pending is not None and pending.kind == "exit" and position is not None:
                self._sell(position, bars[symbol].open, pending.reason, timestamp)
        for symbol in symbols:
            pending = self._pending.get(symbol)
            if pending is not None and pending.kind == "entry":
                self._enter(symbol, bars[symbol].open, pending.stop_distance, timestamp)
        for symbol in symbols:
            self._pending.pop(symbol, None)

        # 2) protective stops (a gap below the stop fills at the open, not at the stop)
        for symbol in symbols:
            position = self.portfolio.positions.get(symbol)
            if position is None or position.stop_price is None:
                continue
            bar = bars[symbol]
            if bar.low <= position.stop_price:
                self._sell(
                    position, min(bar.open, position.stop_price), OrderReason.STOP_LOSS, timestamp
                )

        # 3) mark to market and let the risk engine update its circuit breakers
        for symbol in symbols:
            self.last_close[symbol] = bars[symbol].close
        self.last_bar_time = timestamp
        exposure = self.portfolio.exposure(self.last_close)
        point = EquityPoint(
            timestamp, self.portfolio.cash + exposure, self.portfolio.cash, exposure
        )
        self.equity_curve.append(point)
        self.risk.on_equity_update(timestamp, point.equity)

        # 4) queue the decisions for the next open
        flatten = self.risk.limits.flatten_on_kill_switch and self.risk.kill_switch.is_active_at(
            timestamp
        )
        for symbol in symbols:
            position = self.portfolio.positions.get(symbol)
            signal = signals.get(symbol)
            if position is not None:
                close = bars[symbol].close
                position.highest_close = max(position.highest_close or close, close)
                if signal is not None and math.isfinite(signal.trail_distance):
                    candidate = position.highest_close - signal.trail_distance
                    if position.stop_price is None or candidate > position.stop_price:
                        position.stop_price = candidate
                if flatten:
                    self._pending[symbol] = _Pending("exit", reason=OrderReason.KILL_SWITCH)
                elif signal is not None and signal.exit:
                    self._pending[symbol] = _Pending("exit", reason=OrderReason.EXIT_SIGNAL)
            elif (
                signal is not None
                and signal.entry
                and not signal.exit
                and math.isfinite(signal.stop_distance)
                and signal.stop_distance > 0
            ):
                self._pending[symbol] = _Pending("entry", stop_distance=signal.stop_distance)
        return point

    @property
    def trades(self) -> list[Trade]:
        return self.portfolio.trades

    # ------------------------------------------------------------- persistence
    def snapshot(self) -> dict[str, Any]:
        """Serialisable state so paper trading can resume across process restarts."""
        return {
            "cash": self.portfolio.cash,
            "initial_capital": self.portfolio.initial_capital,
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "entry_time": p.entry_time.isoformat(),
                    "entry_fees": p.entry_fees,
                    "stop_price": p.stop_price,
                    "highest_close": p.highest_close,
                }
                for p in self.portfolio.positions.values()
            ],
            "pending": {
                s: {
                    "kind": p.kind,
                    "stop_distance": p.stop_distance if math.isfinite(p.stop_distance) else None,
                    "reason": p.reason.value,
                }
                for s, p in self._pending.items()
            },
            "last_close": dict(self.last_close),
            "last_bar_time": self.last_bar_time.isoformat() if self.last_bar_time else None,
            "risk": self.risk.snapshot(),
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Inverse of :meth:`snapshot`."""
        self.portfolio.cash = float(state["cash"])
        self.portfolio.positions = {
            p["symbol"]: Position(
                symbol=p["symbol"],
                quantity=float(p["quantity"]),
                avg_price=float(p["avg_price"]),
                entry_time=datetime.fromisoformat(p["entry_time"]),
                entry_fees=float(p["entry_fees"]),
                stop_price=p["stop_price"],
                highest_close=p["highest_close"],
            )
            for p in state["positions"]
        }
        self._pending = {
            s: _Pending(
                p["kind"],
                float(p["stop_distance"]) if p["stop_distance"] is not None else math.nan,
                OrderReason(p["reason"]),
            )
            for s, p in state["pending"].items()
        }
        self.last_close = {s: float(v) for s, v in state["last_close"].items()}
        raw_time = state.get("last_bar_time")
        self.last_bar_time = datetime.fromisoformat(raw_time) if raw_time else None
        self.risk.restore(state["risk"])
