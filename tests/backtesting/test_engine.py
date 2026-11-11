"""Mechanics of the simulation: timing, costs, stops, determinism, no look-ahead."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import run_backtest
from qts.core.config import AppConfig, CostConfig
from qts.core.errors import DataError
from qts.core.types import OrderReason, Side
from qts.strategies.base import Strategy
from tests.conftest import make_candles

SYMBOL = "TEST/USDT"


class ScriptedStrategy(Strategy):
    """Emits entries/exits on fixed bar numbers so the engine can be tested in isolation."""

    name = "scripted"

    def __init__(
        self, entries: set[int], exits: set[int], stop: float = 20.0, trail: float = float("nan")
    ) -> None:
        self.entries, self.exits, self.stop, self.trail = entries, exits, stop, trail

    @property
    def warmup_bars(self) -> int:
        return 0

    def generate_signals(self, candles: pd.DataFrame) -> pd.DataFrame:
        positions = np.arange(len(candles))
        return pd.DataFrame(
            {
                "entry": np.isin(positions, list(self.entries)),
                "exit": np.isin(positions, list(self.exits)),
                "stop_distance": self.stop,
                "trail_distance": self.trail,
            },
            index=candles.index,
        )


def cfg(config: AppConfig, **costs: float) -> AppConfig:
    return config.model_copy(
        update={"costs": CostConfig(**{"fee_rate": 0.0, "slippage_bps": 0.0, **costs})}
    )


def test_signal_at_close_is_executed_at_next_open(config: AppConfig) -> None:
    candles = make_candles([100, 100, 110, 120, 130, 125], spread=0.0)
    result = run_backtest(cfg(config), {SYMBOL: candles}, ScriptedStrategy({1}, {4}))
    buy, sell = result.fills
    assert buy.side is Side.BUY
    assert buy.timestamp == candles.index[2]  # signal on bar 1 -> fill at open of bar 2
    assert buy.price == candles["open"].iloc[2] == 100.0
    assert sell.timestamp == candles.index[5]
    assert sell.price == candles["open"].iloc[5] == 130.0
    assert sell.reason is OrderReason.EXIT_SIGNAL
    # risk 1% of 10k with a 20-wide stop -> 5 units; +30 each
    assert buy.quantity == pytest.approx(5.0)
    assert result.trades[0].pnl == pytest.approx(150.0)
    assert result.equity.iloc[-1] == pytest.approx(10_150.0)


def test_fees_and_slippage_are_charged_on_both_sides(config: AppConfig) -> None:
    candles = make_candles([100, 100, 100, 100, 100, 100], spread=0.0)
    result = run_backtest(
        cfg(config, fee_rate=0.001, slippage_bps=10), {SYMBOL: candles}, ScriptedStrategy({1}, {3})
    )
    buy, sell = result.fills
    assert buy.price == pytest.approx(100.1)
    assert sell.price == pytest.approx(99.9)
    expected = -(buy.quantity * 0.2) - buy.fee - sell.fee
    assert result.trades[0].pnl == pytest.approx(expected)
    assert result.equity.iloc[-1] == pytest.approx(10_000 + expected)
    assert result.equity.iloc[-1] < 10_000  # a flat market must lose exactly the costs


def test_stop_loss_fills_at_stop_price(config: AppConfig) -> None:
    candles = make_candles([100, 100, 100, 100, 100, 100], spread=0.0)
    candles.iloc[3, candles.columns.get_loc("low")] = 75.0  # intraday dip through the stop at 80
    result = run_backtest(cfg(config), {SYMBOL: candles}, ScriptedStrategy({1}, set()))
    stop = result.fills[1]
    assert stop.reason is OrderReason.STOP_LOSS
    assert stop.timestamp == candles.index[3]
    assert stop.price == pytest.approx(80.0)
    assert result.trades[0].pnl == pytest.approx(-100.0)  # exactly the 1% risked


def test_gap_through_stop_fills_at_the_worse_open(config: AppConfig) -> None:
    candles = make_candles([100, 100, 100, 60, 60, 60], spread=0.0)
    candles.iloc[3, candles.columns.get_loc("open")] = 60.0  # gaps below the stop at 80
    candles.iloc[3, candles.columns.get_loc("high")] = 60.0
    result = run_backtest(cfg(config), {SYMBOL: candles}, ScriptedStrategy({1}, set()))
    assert result.fills[1].price == pytest.approx(60.0)
    assert result.trades[0].pnl == pytest.approx(-200.0)  # worse than planned: no fantasy fill


def test_trailing_stop_ratchets_up_only(config: AppConfig) -> None:
    candles = make_candles([100, 100, 100, 130, 160, 150, 138, 138], spread=0.0)
    strategy = ScriptedStrategy({1}, set(), stop=20.0, trail=20.0)
    result = run_backtest(cfg(config), {SYMBOL: candles}, strategy)
    stop = result.fills[1]
    assert stop.reason is OrderReason.TRAILING_STOP  # the ratcheted stop, not the initial one
    assert stop.price == pytest.approx(140.0)  # highest close 160 - 20
    assert stop.timestamp == candles.index[6]


def test_without_signals_capital_is_untouched(config: AppConfig) -> None:
    candles = make_candles(np.linspace(100, 50, 40))
    result = run_backtest(cfg(config), {SYMBOL: candles}, ScriptedStrategy(set(), set()))
    assert (result.equity == 10_000.0).all()
    assert result.decisions == []


def test_open_position_is_marked_to_market_not_force_closed(config: AppConfig) -> None:
    candles = make_candles([100, 100, 100, 110, 120], spread=0.0)
    result = run_backtest(cfg(config), {SYMBOL: candles}, ScriptedStrategy({1}, set()))
    assert result.trades == []
    assert len(result.open_positions) == 1
    assert result.equity.iloc[-1] == pytest.approx(10_000 + 5.0 * 20.0)
    assert result.exposure.iloc[-1] == pytest.approx(600.0)


def test_backtest_is_deterministic(config: AppConfig, walk_data: dict[str, pd.DataFrame]) -> None:
    a = run_backtest(config, walk_data)
    b = run_backtest(config, walk_data)
    pd.testing.assert_series_equal(a.equity, b.equity)
    assert [(t.entry_time, t.exit_time, t.pnl) for t in a.trades] == [
        (t.entry_time, t.exit_time, t.pnl) for t in b.trades
    ]
    assert a.config_fingerprint == b.config_fingerprint == config.fingerprint()
    assert a.config == config.model_dump(mode="json")


def test_no_lookahead_future_data_does_not_change_the_past(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    """Equity up to day N must be identical whether or not days after N exist."""
    full = run_backtest(config, walk_data)
    cut = 900
    truncated = run_backtest(config, {SYMBOL: walk_data[SYMBOL].iloc[:cut]})
    pd.testing.assert_series_equal(truncated.equity, full.equity.iloc[:cut])
    assert len(full.trades) > 0


def test_window_uses_warm_indicators_without_trading_before_start(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    start = walk_data[SYMBOL].index[700]
    result = run_backtest(config, walk_data, start=start)
    assert result.equity.index[0] == start
    assert all(f.timestamp > start for f in result.fills)
    assert len(result.fills) > 0


def test_exposure_never_exceeds_equity_no_leverage(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    result = run_backtest(config, walk_data)
    assert (result.cash >= -1e-6).all()
    assert (result.exposure <= result.equity + 1e-6).all()


def test_empty_data_or_window_raises(config: AppConfig) -> None:
    candles = make_candles([1.0, 2.0, 3.0])
    with pytest.raises(DataError):
        run_backtest(config, {})
    with pytest.raises(DataError, match="no bars"):
        run_backtest(config, {SYMBOL: candles}, start="2030-01-01")


def test_multi_symbol_with_late_listing(config: AppConfig) -> None:
    multi = AppConfig.model_validate(
        {
            "market": {"symbols": ["A/USDT", "B/USDT"]},
            "risk": {"allowed_symbols": None},
            "costs": {"fee_rate": 0.0, "slippage_bps": 0.0},
        }
    )
    a = make_candles([100] * 10, start="2021-01-01", spread=0.0)
    b = make_candles([50] * 5, start="2021-01-06", spread=0.0)
    result = run_backtest(multi, {"A/USDT": a, "B/USDT": b}, ScriptedStrategy({1}, set(), stop=10))
    assert len(result.equity) == 10
    assert {f.symbol for f in result.fills} == {"A/USDT", "B/USDT"}
    assert [f.timestamp for f in result.fills] == [a.index[2], b.index[2]]


def test_buy_and_hold_benchmark(config: AppConfig) -> None:
    candles = make_candles([100, 110, 121], spread=0.0)
    free = buy_and_hold({SYMBOL: candles}, 1000.0, CostConfig(fee_rate=0.0, slippage_bps=0.0))
    assert free.tolist() == pytest.approx([1000.0, 1100.0, 1210.0])
    costly = buy_and_hold({SYMBOL: candles}, 1000.0, CostConfig(fee_rate=0.01, slippage_bps=100))
    assert costly.iloc[0] == pytest.approx(1000.0 / (1.01 * 1.01))


def test_buy_and_hold_equal_weight_and_late_listing() -> None:
    a = make_candles([100, 200, 200, 200], start="2021-01-01", spread=0.0)
    b = make_candles([10, 20], start="2021-01-03", spread=0.0)
    curve = buy_and_hold({"A": a, "B": b}, 1000.0, CostConfig(fee_rate=0.0, slippage_bps=0.0))
    assert curve.tolist() == pytest.approx([1000.0, 1500.0, 1500.0, 2000.0])
