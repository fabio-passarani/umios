"""Realistic execution: spread, impact, volume limits, partial fills, refusals,
delays, gaps and delistings — and the guarantee that the legacy model is untouched."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
import pytest

from qts.backtesting.engine import compute_conditions, run_backtest
from qts.backtesting.metrics import compute_metrics
from qts.core.config import AppConfig, CostConfig, ExecutionConfig
from qts.core.types import OrderReason, Side
from qts.execution.model import (
    LegacyExecution,
    MarketConditions,
    RealisticExecution,
    build_execution,
)
from qts.risk.kill_switch import KillSwitch
from tests.backtesting.test_engine import ScriptedStrategy

SYMBOL = "TEST/USDT"
COSTS = CostConfig(fee_rate=0.001, slippage_bps=5.0)
DEEP = MarketConditions(traded_value=1e9, volatility=0.03, candle_value=1e9)
# thin on average (wide spread, large impact) but with a candle big enough to fill the order
THIN = MarketConditions(traded_value=2e5, volatility=0.08, candle_value=1e9)


def model(**overrides: Any) -> RealisticExecution:
    return RealisticExecution(ExecutionConfig(**overrides), COSTS)


def candles(closes: list[float], value: float = 1e9, freq: str = "D") -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2021-01-01", periods=len(close), freq=freq, tz="UTC", name="timestamp")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": value / close,
            "quote_volume": value,
        },
        index=index,
    )


def config(**execution: Any) -> AppConfig:
    return AppConfig.model_validate(
        {
            "market": {"symbols": [SYMBOL]},
            "risk": {"allowed_symbols": [SYMBOL]},
            "costs": {"fee_rate": 0.001, "slippage_bps": 5.0},
            "execution": {"stats_window_days": 5, **execution},
        }
    )


FLAT = [100.0] * 8


# ------------------------------------------------------------------ the model itself
def test_legacy_model_is_the_old_formula() -> None:
    legacy = build_execution(None, COSTS)
    assert isinstance(legacy, LegacyExecution)
    buy = legacy.execute(Side.BUY, 2.0, 100.0, None)
    sell = legacy.execute(Side.SELL, 2.0, 100.0, THIN)  # conditions are ignored
    assert (buy.price, sell.price) == (100.0 * 1.0005, 100.0 * 0.9995)
    assert buy.fee == 2.0 * buy.price * 0.001
    assert buy.quantity == sell.quantity == 2.0
    assert legacy.cost_bound(None) == COSTS.cost_rate
    assert AppConfig().fingerprint() != config().fingerprint()  # a different experiment


def test_spread_widens_as_liquidity_falls_and_is_capped() -> None:
    m = model()
    values = [1e9, 1e8, 1e7, 1e6, 1e5, 1e3, 1.0]
    spreads = [m.half_spread(MarketConditions(traded_value=v, volatility=0.03)) for v in values]
    assert spreads == sorted(spreads)
    assert spreads[0] == pytest.approx(729.0 * 1e9**-0.379 / 1e4)  # about 0.3 bps
    assert spreads[-1] == 200.0 / 1e4  # the cap
    assert m.half_spread(None) == 200.0 / 1e4  # nothing known: assume the worst
    assert m.half_spread(MarketConditions()) == 200.0 / 1e4
    assert model(spread_multiplier=3.0).half_spread(DEEP) == pytest.approx(3 * m.half_spread(DEEP))
    assert model(liquidity_scale=0.01).half_spread(DEEP) > m.half_spread(DEEP)


def test_impact_follows_the_square_root_law() -> None:
    m = model()
    base = m.impact(10_000.0, THIN)
    assert base == pytest.approx(0.5 * 0.08 * math.sqrt(10_000.0 / 2e5))
    assert m.impact(40_000.0, THIN) == pytest.approx(2 * base)  # 4x the order, 2x the impact
    assert m.impact(10_000.0, DEEP) < base / 50
    assert model(impact_coefficient=2.0).impact(10_000.0, THIN) == pytest.approx(4 * base)
    assert m.impact(10_000.0, None) == 0.0
    assert m.impact(10_000.0, MarketConditions(traded_value=1e6)) == 0.0  # volatility unknown


def test_costs_are_always_adverse_and_reported_by_component() -> None:
    m = model()
    buy = m.execute(Side.BUY, 100.0, 50.0, THIN)
    sell = m.execute(Side.SELL, 100.0, 50.0, THIN)
    assert buy.price > 50.0 > sell.price
    assert buy.price - 50.0 == pytest.approx(50.0 - sell.price)
    value = 100.0 * 50.0
    assert buy.spread_cost == pytest.approx(value * m.half_spread(THIN))
    assert buy.impact_cost == pytest.approx(value * m.impact(value, THIN))
    assert buy.quantity * (buy.price - 50.0) == pytest.approx(buy.spread_cost + buy.impact_cost)
    assert buy.fee == pytest.approx(buy.quantity * buy.price * 0.001)
    # the planning figure covers an order limited to its share of a typical day's volume
    typical = MarketConditions(traded_value=2e5, volatility=0.08, candle_value=2e5)
    capped = m.execute(Side.BUY, 100.0, 50.0, typical)
    assert capped.status == "partial"
    assert m.cost_bound(typical) >= (capped.price / 50.0) * 1.001 - 1.0 - 1e-12


def test_an_order_cannot_take_more_than_its_share_of_the_volume() -> None:
    m = model(max_participation=0.01)
    outcome = m.execute(Side.BUY, 100.0, 50.0, MarketConditions(1e6, 0.05, candle_value=100_000.0))
    assert outcome.status == "partial"
    assert outcome.quantity == pytest.approx(0.01 * 100_000.0 / 50.0)  # 1% of what traded
    assert m.execute(Side.BUY, 1.0, 50.0, DEEP).status == "filled"
    thinner = model(max_participation=0.01, liquidity_scale=0.1)
    assert thinner.execute(
        Side.BUY, 100.0, 50.0, MarketConditions(1e6, 0.05, candle_value=100_000.0)
    ).quantity == pytest.approx(outcome.quantity / 10)


def test_orders_below_the_minimum_value_and_random_refusals() -> None:
    m = model(min_notional=5.0)
    tiny = m.execute(Side.BUY, 0.01, 100.0, DEEP)
    assert (tiny.status, tiny.quantity) == ("rejected", 0.0)
    dust = m.execute(Side.SELL, 10.0, 100.0, MarketConditions(1e6, 0.05, candle_value=10.0))
    assert dust.status == "rejected"  # 1% of a candle that barely traded is below the minimum

    refusing = model(rejection_rate=0.3, seed=7)
    outcomes = [refusing.execute(Side.BUY, 1.0, 100.0, DEEP).status for _ in range(2000)]
    assert 0.25 < outcomes.count("rejected") / 2000 < 0.35
    again = model(rejection_rate=0.3, seed=7)
    assert [again.execute(Side.BUY, 1.0, 100.0, DEEP).status for _ in range(2000)] == outcomes
    forced = refusing.execute(Side.SELL, 1.0, 100.0, None, forced=True)
    assert (forced.status, forced.price) == ("filled", 100.0)  # a liquidation is not refused


# ---------------------------------------------------------------- inside the engine
def test_conditions_use_only_earlier_candles() -> None:
    cfg = config()
    frame = candles([100 + i for i in range(40)], value=1e6)
    frame.loc[frame.index[20:], "quote_volume"] *= 100
    table = compute_conditions(cfg, {SYMBOL: frame})[SYMBOL]
    assert table["traded_value"].iloc[20] == pytest.approx(1e6)  # the jump is not known yet
    assert table["candle_value"].iloc[20] == pytest.approx(1e8)  # but it is what the order meets
    assert table["traded_value"].iloc[30] == pytest.approx(1e8)
    cut = compute_conditions(cfg, {SYMBOL: frame.iloc[:25]})[SYMBOL]
    pd.testing.assert_frame_equal(cut, table.iloc[:25])
    assert compute_conditions(AppConfig(), {SYMBOL: frame}) == {}  # legacy: nothing computed


def test_realistic_costs_reduce_the_result_and_never_create_leverage() -> None:
    closes = [100.0] * 3 + [110, 120, 130, 125, 140, 150, 145]
    strategy = ScriptedStrategy({1, 6}, {4, 8}, stop=30.0)
    results = {}
    for name, value in (("deep", 1e10), ("thin", 3e5)):
        r = run_backtest(config(), {SYMBOL: candles(closes, value)}, strategy)
        results[name] = r
        assert (r.cash >= -1e-9).all()
        assert (r.exposure <= r.equity + 1e-6).all()
        assert all(d.approved for d in r.decisions)
    free = run_backtest(
        config(half_spread_coefficient_bps=1e-9, impact_coefficient=0.0).model_copy(
            update={"costs": CostConfig(fee_rate=0.0, slippage_bps=0.0)}
        ),
        {SYMBOL: candles(closes, 3e5)},
        strategy,
    )
    assert free.equity.iloc[-1] > results["deep"].equity.iloc[-1] > results["thin"].equity.iloc[-1]
    metrics = compute_metrics(
        results["thin"].equity, 10_000.0, results["thin"].trades, results["thin"].exposure,
        fills=results["thin"].fills,
    )  # fmt: skip
    assert metrics["total_costs"] == pytest.approx(
        metrics["cost_fees"] + metrics["cost_spread"] + metrics["cost_impact"]
    )
    assert metrics["cost_spread"] > 0
    assert metrics["cost_impact"] > 0
    assert metrics["turnover"] > 0
    assert metrics["cost_per_traded_bps"] > 10  # more than the fee alone
    assert 0 < metrics["average_exposure"] < 1


def test_a_thin_market_fills_an_entry_partially_and_an_exit_over_several_candles() -> None:
    """1% of a candle that traded 20 000: at most 200 of value per candle."""
    closes = [100.0] * 14
    result = run_backtest(
        config(max_participation=0.01),
        {SYMBOL: candles(closes, 20_000.0)},
        ScriptedStrategy({1}, {4}, stop=30.0),
    )
    buy = result.fills[0]
    assert buy.quantity == pytest.approx(2.0)  # 200 / 100, although about 300 worth was wanted
    assert buy.requested_quantity > buy.quantity
    sells = [f for f in result.fills if f.side is Side.SELL]
    assert len(sells) == 1  # the position was small enough to leave in one candle
    events = [e for e in result.execution_events if e.status == "partial"]
    assert events
    assert events[0].side is Side.BUY

    thin_exit = run_backtest(
        config(max_participation=0.01),
        {SYMBOL: candles(closes, 20_000.0).assign(quote_volume=[1e9] * 4 + [5_000.0] * 10)},
        ScriptedStrategy({1}, {3}, stop=30.0),
    )
    sells = [f for f in thin_exit.fills if f.side is Side.SELL]
    assert len(sells) > 3  # the exit takes several candles of 50 each ...
    assert [f.timestamp for f in sells] == sorted(
        {f.timestamp for f in sells}
    )  # ... one per candle
    assert all(f.reason is OrderReason.EXIT_SIGNAL for f in sells)
    bought = sum(f.quantity for f in thin_exit.fills if f.side is Side.BUY)
    assert sum(f.quantity for f in sells) <= bought + 1e-9  # never more than was held
    assert all(d.approved for d in thin_exit.decisions if d.order.side is Side.SELL)


class Refusing(RealisticExecution):
    """Refuses exactly the orders whose sequence numbers are listed."""

    def __init__(self, refuse: set[int]) -> None:
        super().__init__(ExecutionConfig(stats_window_days=5), COSTS)
        self.refuse, self.calls = refuse, 0

    def execute(self, side, quantity, market_price, conditions, forced=False):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls in self.refuse:
            from qts.execution.model import ExecutionOutcome

            return ExecutionOutcome(0.0, market_price, 0.0, status="rejected", detail="refused")
        return super().execute(side, quantity, market_price, conditions, forced)


def _run_with(
    execution: RealisticExecution, closes: list[float], strategy: ScriptedStrategy
) -> Any:
    from qts.backtesting.engine import build_session, compute_signal_frames, iter_bars
    from qts.core.types import TradingMode

    data = {SYMBOL: candles(closes)}
    session = build_session(config(), strategy, TradingMode.BACKTEST)
    session.execution = session.broker.execution = execution
    signals = compute_signal_frames(strategy, data)
    for timestamp, bars, rows in iter_bars(data, signals, None, None):
        session.process_bar(timestamp.to_pydatetime(), bars, rows, {SYMBOL: DEEP})
    return session, data[SYMBOL].index


def test_a_refused_entry_is_not_chased_and_a_refused_exit_is_retried() -> None:
    closes = [100.0] * 16
    strategy = ScriptedStrategy({1, 6}, {9}, stop=30.0)
    # order 1: the first entry (refused). order 2: the second entry (filled).
    # orders 3 and 4: the exit, refused twice. order 5: the exit, executed.
    session, days = _run_with(Refusing({1, 3, 4}), closes, strategy)
    fills = session.portfolio.fills
    assert [(f.side, f.timestamp) for f in fills] == [
        (Side.BUY, days[7]),  # the entry of bar 2 was refused and never sent again
        (Side.SELL, days[12]),  # the exit was due on bar 10: refused on 10 and 11, done on 12
    ]
    assert session.portfolio.positions == {}
    refused = session.broker.events
    assert [(e.side, e.status, e.timestamp) for e in refused] == [
        (Side.BUY, "rejected", days[2]),
        (Side.SELL, "rejected", days[10]),
        (Side.SELL, "rejected", days[11]),
    ]
    decisions = session.risk.journal.decisions
    assert len(decisions) == 5  # each attempt was authorised again by the risk engine
    assert all(d.approved for d in decisions)


def test_delay_in_candles_moves_the_fill_to_a_later_open() -> None:
    closes = [100.0, 100, 105, 110, 120, 130, 140, 150]
    data = {SYMBOL: candles(closes)}
    strategy = ScriptedStrategy({1}, {4}, stop=60.0)
    prompt = run_backtest(config(), data, strategy)
    late = run_backtest(config(delay_bars=1), data, strategy)
    assert prompt.fills[0].timestamp == data[SYMBOL].index[2]
    assert late.fills[0].timestamp == data[SYMBOL].index[3]  # one candle later
    assert late.fills[0].price > prompt.fills[0].price  # and at a worse price in a rising market
    assert late.fills[1].timestamp == data[SYMBOL].index[6]  # the exit is one candle late too
    assert prompt.fills[1].timestamp == data[SYMBOL].index[5]


def test_delay_in_hours_fills_at_the_hourly_price_actually_reached() -> None:
    closes = [100.0, 100, 100, 110, 120, 130]
    daily = candles(closes)
    hours = pd.date_range(daily.index[0], periods=24 * len(closes), freq="h", tz="UTC")
    hourly = pd.DataFrame({"open": 100.0 + np.arange(len(hours)) * 0.1}, index=hours)
    strategy = ScriptedStrategy({1}, set(), stop=60.0)
    result = run_backtest(
        config(delay_hours=4), {SYMBOL: daily}, strategy, intraday={SYMBOL: hourly}
    )
    entry = result.fills[0]
    reached = hourly["open"].loc[daily.index[2] + pd.Timedelta(hours=4)]
    assert entry.timestamp == daily.index[2]
    assert entry.price == pytest.approx(reached, rel=1e-3)  # the 04:00 price, plus tiny costs
    assert entry.price > daily["open"].iloc[2] * 1.001
    without = run_backtest(config(delay_hours=4), {SYMBOL: daily}, strategy)  # no hourly data
    assert without.fills[0].price == pytest.approx(daily["open"].iloc[2], rel=1e-3)


def test_a_position_in_an_asset_that_stops_trading_is_liquidated_with_a_haircut() -> None:
    alive = candles([50.0] * 12)
    dead = candles([100.0, 100, 100, 110, 120, 130])  # no candle after the sixth day
    cfg = AppConfig.model_validate(
        {
            "market": {"symbols": ["DEAD/USDT", "LIVE/USDT"]},
            "risk": {"allowed_symbols": ["DEAD/USDT", "LIVE/USDT"]},
            "execution": {"stats_window_days": 5, "delisting_haircut": 0.25},
        }
    )

    class OnlyDead(ScriptedStrategy):
        def generate_signals(self, frame: pd.DataFrame) -> pd.DataFrame:
            signals = super().generate_signals(frame)
            if len(frame) > 6:  # the surviving asset never trades
                signals["entry"] = False
            return signals

    result = run_backtest(cfg, {"DEAD/USDT": dead, "LIVE/USDT": alive}, OnlyDead({1}, set(), 60.0))
    exit_ = result.fills[-1]
    assert exit_.reason is OrderReason.DELISTING
    assert exit_.symbol == "DEAD/USDT"
    assert exit_.price == pytest.approx(130.0 * 0.75)  # last price, minus the haircut
    assert exit_.timestamp == alive.index[6]  # the first candle after it vanished
    assert not result.open_positions
    assert result.exposure.iloc[-1] == 0.0  # no phantom value carried at a stale price
    assert result.trades[-1].exit_reason is OrderReason.DELISTING
    assert result.trades[-1].pnl < 0
    assert result.decisions[-1].approved  # through the reduce-only path, journaled


def test_the_risk_engine_keeps_its_authority_under_the_realistic_model() -> None:
    from qts.backtesting.engine import build_session, compute_signal_frames, iter_bars
    from qts.core.types import TradingMode

    cfg = config()
    data = {SYMBOL: candles([100.0] * 3 + [110, 120, 130, 140])}
    strategy = ScriptedStrategy({1, 3}, set(), stop=60.0)
    switch = KillSwitch()
    switch.activate("operator stop", source="operator")
    session = build_session(cfg, strategy, TradingMode.BACKTEST, switch)
    signals = compute_signal_frames(strategy, data)
    for timestamp, bars, rows in iter_bars(data, signals, None, None):
        session.process_bar(timestamp.to_pydatetime(), bars, rows, {SYMBOL: DEEP})
    assert session.portfolio.fills == []
    assert session.risk.journal.decisions
    assert all(not d.approved for d in session.risk.journal.decisions)


def test_a_missing_hour_delays_the_order_to_the_next_traded_hour() -> None:
    from qts.backtesting.engine import _delayed_opens

    days = pd.date_range("2021-01-01", periods=2, freq="D", tz="UTC")
    hours = pd.DatetimeIndex(
        [days[0], days[0] + pd.Timedelta(hours=2), days[0] + pd.Timedelta(hours=3), days[1]]
    )
    opens = pd.Series([1.0, 2.0, 3.0, 4.0], index=hours)
    found = _delayed_opens(days, opens, 1, pd.Timedelta(days=1))
    # day 1: 01:00 is missing -> 02:00, never the earlier 00:00 open
    assert found[0] == 2.0
    # day 2: nothing traded after 00:00 within the day -> unknown, not a stale price
    assert math.isnan(found[1])
