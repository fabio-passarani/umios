import numpy as np
import pandas as pd
import pytest

from qts.core.config import TrendFollowingParams
from qts.strategies import SIGNAL_COLUMNS, TrendFollowingStrategy
from qts.strategies.indicators import atr, donchian_high, donchian_low, ema, true_range
from tests.conftest import make_candles, random_walk

PARAMS = TrendFollowingParams(
    breakout_window=5, exit_window=3, fast_ema=3, slow_ema=6, atr_window=3, max_atr_pct=0.5
)


def test_ema_matches_recursive_definition() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0])
    result = ema(series, 2)
    alpha = 2 / 3
    expected = 1.0
    for value in [2.0]:
        expected = alpha * value + (1 - alpha) * expected
    assert np.isnan(result.iloc[0])
    assert result.iloc[1] == pytest.approx(expected)


def test_true_range_uses_gaps() -> None:
    high, low, close = pd.Series([10.0, 15.0]), pd.Series([9.0, 14.0]), pd.Series([9.5, 14.5])
    assert true_range(high, low, close).tolist() == [1.0, 5.5]  # gap from 9.5 to 15


def test_atr_is_wilder_average() -> None:
    high = pd.Series([2.0, 2.0, 2.0, 4.0])
    low = pd.Series([1.0, 1.0, 1.0, 1.0])
    close = pd.Series([1.5, 1.5, 1.5, 1.5])
    result = atr(high, low, close, 2)
    assert np.isnan(result.iloc[0])
    assert result.iloc[2] == pytest.approx(1.0)
    assert result.iloc[3] == pytest.approx(0.5 * 3.0 + 0.5 * 1.0)


def test_donchian_channels_exclude_current_bar() -> None:
    high = pd.Series([1.0, 2.0, 3.0, 10.0])
    low = pd.Series([1.0, 0.5, 3.0, 0.1])
    assert donchian_high(high, 2).iloc[3] == 3.0  # not 10: today's bar is excluded
    assert donchian_low(low, 2).iloc[3] == 0.5


def test_signal_frame_shape_and_warmup() -> None:
    strategy = TrendFollowingStrategy(PARAMS)
    candles = make_candles(np.linspace(100, 120, 30))
    signals = strategy.generate_signals(candles)
    assert set(SIGNAL_COLUMNS) <= set(signals.columns)
    assert signals.index.equals(candles.index)
    assert signals["entry"].dtype == bool
    assert signals["exit"].dtype == bool
    assert not signals["entry"].iloc[: PARAMS.breakout_window].any()
    assert signals["trail_distance"].isna().all()  # trailing disabled by default


def test_breakout_in_uptrend_triggers_entry_and_breakdown_triggers_exit() -> None:
    closes = [100.0] * 10 + [101, 102, 103, 104, 110] + [109, 108, 90, 85]
    signals = TrendFollowingStrategy(PARAMS).generate_signals(make_candles(closes, spread=0.001))
    assert signals["entry"].iloc[14]  # close 110 clears the 5-day high in an uptrend
    assert not signals["entry"].iloc[:10].any()  # flat market: no breakout
    assert signals["exit"].iloc[17]  # close 90 breaks the 3-day low


def test_no_entry_in_downtrend_even_on_breakout() -> None:
    closes = [*np.linspace(200, 100, 30), 101, 102, 103, 104, 105, 112]
    signals = TrendFollowingStrategy(PARAMS.model_copy(update={"slow_ema": 25})).generate_signals(
        make_candles(closes, spread=0.001)
    )
    assert not signals["entry"].iloc[-1]  # fast EMA still below slow EMA


def test_volatility_filter_blocks_entries() -> None:
    closes = [100.0] * 10 + [101, 102, 103, 104, 110]
    candles = make_candles(closes, spread=0.001)
    calm = TrendFollowingStrategy(PARAMS).generate_signals(candles)
    strict = TrendFollowingStrategy(
        PARAMS.model_copy(update={"max_atr_pct": 0.001})
    ).generate_signals(candles)
    assert calm["entry"].any()
    assert not strict["entry"].any()


def test_trailing_distance_emitted_when_enabled() -> None:
    params = PARAMS.model_copy(update={"trailing_stop": True, "trailing_atr_multiple": 2.0})
    signals = TrendFollowingStrategy(params).generate_signals(make_candles(np.linspace(1, 2, 20)))
    valid = signals.dropna(subset=["atr"])
    assert (valid["trail_distance"] == valid["atr"] * 2.0).all()


def test_no_lookahead_signals_do_not_change_when_future_is_appended() -> None:
    """The core anti-look-ahead guarantee: a signal at t depends only on data <= t."""
    strategy = TrendFollowingStrategy()
    candles = random_walk(600, seed=3)
    full = strategy.generate_signals(candles)
    for cut in (250, 400, 599):
        partial = strategy.generate_signals(candles.iloc[:cut])
        pd.testing.assert_frame_equal(partial, full.iloc[:cut])


def test_no_lookahead_future_shock_does_not_alter_past_signals() -> None:
    strategy = TrendFollowingStrategy()
    candles = random_walk(500, seed=5)
    shocked = candles.copy()
    shocked.iloc[300:] *= 10.0
    a = strategy.generate_signals(candles).iloc[:300]
    b = strategy.generate_signals(shocked).iloc[:300]
    pd.testing.assert_frame_equal(a, b)


def test_invalid_parameters_rejected() -> None:
    with pytest.raises(ValueError, match="fast_ema"):
        TrendFollowingParams(fast_ema=200, slow_ema=50)
    with pytest.raises(ValueError, match="min_atr_pct"):
        TrendFollowingParams(min_atr_pct=0.2, max_atr_pct=0.1)
