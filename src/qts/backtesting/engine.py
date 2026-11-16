"""Historical simulation driver.

The engine itself is thin: it pre-computes the strategy's (causal) signals,
then feeds bars one at a time to :class:`~qts.execution.session.TradingSession`,
the same object paper trading uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from qts.core.config import AppConfig
from qts.core.errors import DataError
from qts.core.provenance import build_provenance
from qts.core.types import Fill, Trade, TradingMode
from qts.execution.broker import ExecutionEvent
from qts.execution.model import MarketConditions, build_execution
from qts.execution.session import Bar, SignalRow, TradingSession
from qts.market_data.schema import TIMEFRAME_TO_FREQ, bars_per_day
from qts.portfolio.portfolio import Position
from qts.portfolio.sizing import PositionSizer
from qts.risk.engine import RiskEngine
from qts.risk.journal import RiskEvent, RiskJournal
from qts.risk.kill_switch import KillSwitch
from qts.risk.models import RiskDecision
from qts.strategies import Strategy, build_strategy


@dataclass
class BacktestResult:
    """Everything a run produced, plus what is needed to reproduce it."""

    equity: pd.Series
    cash: pd.Series
    exposure: pd.Series
    trades: list[Trade]
    fills: list[Fill]
    decisions: list[RiskDecision]
    risk_events: list[RiskEvent]
    open_positions: list[Position]
    initial_capital: float
    config: dict[str, Any]
    config_fingerprint: str
    kill_switch_active: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Orders the venue refused or only partly executed (realistic execution model).
    execution_events: list[ExecutionEvent] = field(default_factory=list)

    @property
    def returns(self) -> pd.Series:
        """Per-bar returns of the equity curve (first bar measured from initial capital)."""
        previous = self.equity.shift(1)
        previous.iloc[0] = self.initial_capital
        return (self.equity / previous - 1.0).rename("returns")


def build_session(
    config: AppConfig,
    strategy: Strategy,
    mode: TradingMode,
    kill_switch: KillSwitch | None = None,
    journal: RiskJournal | None = None,
) -> TradingSession:
    """Wire sizer, risk engine and simulated broker from a configuration."""
    risk_engine = RiskEngine(config.risk, mode, kill_switch=kill_switch, journal=journal)
    return TradingSession(
        strategy_name=strategy.name,
        sizer=PositionSizer(config.portfolio.sizing),
        risk_engine=risk_engine,
        costs=config.costs,
        initial_capital=config.portfolio.initial_capital,
        bar_duration=TIMEFRAME_TO_FREQ[config.market.timeframe],
        execution=build_execution(config.execution, config.costs),
    )


def compute_conditions(
    config: AppConfig,
    data: dict[str, pd.DataFrame],
    intraday: dict[str, pd.DataFrame] | None = None,
) -> dict[str, pd.DataFrame]:
    """What the execution model may know about each market at each candle.

    * ``traded_value`` — median daily traded value (quote currency) over the
      trailing window, **ending at the previous candle**;
    * ``volatility`` — daily volatility of returns over the same window, likewise
      shifted by one candle;
    * ``candle_value`` — what traded in the candle itself (the capacity an order
      meets; it is not used to decide anything);
    * ``delayed_price`` — with ``execution.delay_hours`` and hourly candles in
      ``intraday``: the open of the hourly candle that many hours after the
      candle's own open. NaN where that hour is missing.

    Returns an empty mapping when the configuration uses the legacy model.
    """
    cfg = config.execution
    if cfg is None:
        return {}
    per_day = bars_per_day(config.market.timeframe)
    window = cfg.stats_window_days * per_day
    out: dict[str, pd.DataFrame] = {}
    for symbol, frame in data.items():
        value = (
            frame["quote_volume"] if "quote_volume" in frame else frame["volume"] * frame["close"]
        )
        daily_value = value.rolling(per_day).sum() if per_day > 1 else value
        table = pd.DataFrame(index=frame.index)
        table["traded_value"] = (
            daily_value.rolling(window, min_periods=window // 3).median().shift(1)
        )
        returns = frame["close"].pct_change()
        table["volatility"] = (
            returns.rolling(window, min_periods=window // 3).std() * np.sqrt(per_day)
        ).shift(1)
        table["candle_value"] = value
        table["delayed_price"] = np.nan
        hourly = (intraday or {}).get(symbol)
        if cfg.delay_hours > 0 and hourly is not None:
            reached = pd.DatetimeIndex(frame.index) + pd.Timedelta(hours=cfg.delay_hours)
            table["delayed_price"] = hourly["open"].reindex(reached).to_numpy()
        out[symbol] = table
    return out


def _conditions_at(
    tables: dict[str, dict[str, np.ndarray[Any, Any]]],
    positions: dict[str, dict[pd.Timestamp, int]],
    timestamp: pd.Timestamp,
    symbols: Any,
) -> dict[str, MarketConditions]:
    found: dict[str, MarketConditions] = {}
    for symbol in symbols:
        row = positions.get(symbol, {}).get(timestamp)
        if row is None:
            continue
        columns = tables[symbol]
        late = float(columns["delayed_price"][row])
        found[symbol] = MarketConditions(
            traded_value=float(columns["traded_value"][row]),
            volatility=float(columns["volatility"][row]),
            candle_value=float(columns["candle_value"][row]),
            delayed_price=late if np.isfinite(late) and late > 0 else None,
        )
    return found


def compute_signal_frames(
    strategy: Strategy, data: dict[str, pd.DataFrame]
) -> dict[str, pd.DataFrame]:
    """Run the strategy on each symbol's full history (indicators are causal)."""
    return {symbol: strategy.generate_signals(frame) for symbol, frame in data.items()}


def iter_bars(
    data: dict[str, pd.DataFrame],
    signals: dict[str, pd.DataFrame],
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
) -> list[tuple[pd.Timestamp, dict[str, Bar], dict[str, SignalRow]]]:
    """Align all symbols on one timeline restricted to ``[start, end]``."""
    index = pd.DatetimeIndex(sorted(set().union(*(frame.index for frame in data.values()))))
    if start is not None:
        index = index[index >= start]
    if end is not None:
        index = index[index <= end]
    columns: dict[str, tuple[np.ndarray[Any, Any], ...]] = {}
    for symbol, frame in data.items():
        candles = frame.reindex(index)
        sig = signals[symbol].reindex(index)
        columns[symbol] = (
            candles["open"].to_numpy(float),
            candles["high"].to_numpy(float),
            candles["low"].to_numpy(float),
            candles["close"].to_numpy(float),
            sig["entry"].fillna(False).to_numpy(bool),
            sig["exit"].fillna(False).to_numpy(bool),
            sig["stop_distance"].to_numpy(float),
            sig["trail_distance"].to_numpy(float),
        )
    timeline: list[tuple[pd.Timestamp, dict[str, Bar], dict[str, SignalRow]]] = []
    for i, timestamp in enumerate(index):
        bars: dict[str, Bar] = {}
        rows: dict[str, SignalRow] = {}
        for symbol, (o, h, low, c, entry, exit_, stop, trail) in columns.items():
            if np.isnan(o[i]):
                continue
            bars[symbol] = Bar(float(o[i]), float(h[i]), float(low[i]), float(c[i]))
            rows[symbol] = SignalRow(
                bool(entry[i]), bool(exit_[i]), float(stop[i]), float(trail[i])
            )
        if bars:
            timeline.append((timestamp, bars, rows))
    return timeline


def run_backtest(
    config: AppConfig,
    data: dict[str, pd.DataFrame],
    strategy: Strategy | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    signals: dict[str, pd.DataFrame] | None = None,
    intraday: dict[str, pd.DataFrame] | None = None,
) -> BacktestResult:
    """Simulate ``strategy`` on ``data``.

    ``intraday`` (hourly candles per symbol) is only needed for an execution
    delay expressed in hours.

    ``start``/``end`` restrict the *trading* window; indicators are still
    computed on all earlier history, so a window does not waste its first
    ``warmup_bars`` and — because indicators only look backwards — no
    information from after a bar can reach it.
    """
    if not data or any(frame.empty for frame in data.values()):
        raise DataError("backtest needs non-empty data for every symbol")
    strategy = strategy or build_strategy(config.strategy)
    signals = signals or compute_signal_frames(strategy, data)
    lo = _utc(start) if start is not None else None
    hi = _utc(end) if end is not None else None
    timeline = iter_bars(data, signals, lo, hi)
    if not timeline:
        raise DataError("no bars in the requested backtest window")

    session = build_session(config, strategy, TradingMode.BACKTEST)
    frames = compute_conditions(config, data, intraday)
    tables = {s: {c: f[c].to_numpy(float) for c in f.columns} for s, f in frames.items()}
    rows_of = {s: {ts: i for i, ts in enumerate(f.index)} for s, f in frames.items()}
    last_candle = {symbol: frame.index[-1] for symbol, frame in data.items()}
    for timestamp, bars, rows in timeline:
        # an asset whose candles ended while a position was open is liquidated first
        for symbol in [s for s in session.portfolio.positions if last_candle[s] < timestamp]:
            session.liquidate_delisted(symbol, timestamp.to_pydatetime())
        conditions = _conditions_at(tables, rows_of, timestamp, bars) if tables else None
        session.process_bar(timestamp.to_pydatetime(), bars, rows, conditions)

    index = pd.DatetimeIndex([p.timestamp for p in session.equity_curve], name="timestamp")
    return BacktestResult(
        equity=pd.Series([p.equity for p in session.equity_curve], index=index, name="equity"),
        cash=pd.Series([p.cash for p in session.equity_curve], index=index, name="cash"),
        exposure=pd.Series(
            [p.exposure for p in session.equity_curve], index=index, name="exposure"
        ),
        trades=list(session.portfolio.trades),
        fills=list(session.portfolio.fills),
        decisions=list(session.risk.journal.decisions),
        risk_events=list(session.risk.journal.events),
        execution_events=list(session.broker.events),
        open_positions=list(session.portfolio.positions.values()),
        initial_capital=config.portfolio.initial_capital,
        config=config.model_dump(mode="json"),
        config_fingerprint=config.fingerprint(),
        kill_switch_active=session.risk.kill_switch.active,
        metadata={
            "strategy": strategy.name,
            "strategy_version": strategy.version,
            "symbols": list(data),
            "start": index[0].isoformat(),
            "end": index[-1].isoformat(),
            "bars": len(index),
        },
    )


def provenance_of(result: BacktestResult, data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Provenance record of a finished run (see :mod:`qts.core.provenance`).

    ``data`` must be the frames the run was given: their content hash becomes
    the data version. Computed on demand, not inside :func:`run_backtest`, so
    studies that run hundreds of backtests do not pay for it each time.
    """
    return build_provenance(
        config=result.config,
        fingerprint=result.config_fingerprint,
        data=data,
        equity=result.equity,
        strategy_name=str(result.metadata.get("strategy", "unknown")),
        strategy_version=str(result.metadata.get("strategy_version", "0")),
    )


def _utc(value: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
