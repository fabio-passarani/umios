"""The engine on 1h, 4h and 1d candles: same mechanics, comparable annualised figures."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import run_backtest
from qts.backtesting.metrics import compute_metrics, sharpe_ratio
from qts.backtesting.validation import run_validation
from qts.core.config import AppConfig, CostConfig
from qts.core.errors import ConfigError, DataError
from qts.core.types import OrderReason, Side, TradingMode
from qts.integrations.freqtrade.bridge import FreqtradeBridge
from qts.market_data.schema import (
    bar_duration,
    bars_per_day,
    check_quality,
    normalize_ohlcv,
    periods_per_year,
)
from tests.backtesting.test_engine import ScriptedStrategy

SYMBOL = "TEST/USDT"


def candles(closes: np.ndarray, freq: str, start: str = "2021-01-01") -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range(start, periods=len(close), freq=freq, tz="UTC", name="timestamp")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.002,
            "low": np.minimum(open_, close) * 0.998,
            "close": close,
            "volume": 1000.0,
        },
        index=index,
    )


def config_for(timeframe: str, **extra: object) -> AppConfig:
    return AppConfig.model_validate(
        {
            "market": {"symbols": [SYMBOL], "timeframe": timeframe},
            "risk": {"allowed_symbols": [SYMBOL]},
            "costs": {"fee_rate": 0.0, "slippage_bps": 0.0},
            **extra,
        }
    )


def test_timeframe_table() -> None:
    assert [periods_per_year(t) for t in ("1d", "4h", "1h")] == [365, 2190, 8760]
    assert [bars_per_day(t) for t in ("1d", "4h", "1h")] == [1, 6, 24]
    assert bar_duration("4h") == pd.Timedelta(hours=4)
    with pytest.raises(DataError, match="not supported"):
        bar_duration("15m")
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic wraps the message
        AppConfig.model_validate({"market": {"timeframe": "15m"}})
    assert config_for("1h").periods_per_year == 8760
    assert (
        AppConfig().fingerprint()
        == AppConfig.model_validate({"market": {"timeframe": "1d"}}).fingerprint()
    )  # daily stays the default: existing fingerprints are unchanged


@pytest.mark.parametrize(("timeframe", "freq"), [("1h", "h"), ("4h", "4h"), ("1d", "D")])
def test_next_open_fills_and_stops_on_every_timeframe(timeframe: str, freq: str) -> None:
    closes = np.array([100, 100, 100, 110, 120, 80, 80, 80], dtype=float)
    data = {SYMBOL: candles(closes, freq)}
    result = run_backtest(config_for(timeframe), data, ScriptedStrategy({1}, set(), stop=15.0))
    buy, sell = result.fills
    assert buy.side is Side.BUY
    assert buy.timestamp == data[SYMBOL].index[2]  # the candle after the signal
    assert buy.price == 100.0
    assert sell.reason is OrderReason.STOP_LOSS
    assert sell.timestamp == data[SYMBOL].index[5]
    assert sell.price == pytest.approx(85.0)  # the stop level: no gap at the open
    assert len(result.equity) == len(closes)
    assert all(d.approved for d in result.decisions)


@pytest.mark.parametrize(("timeframe", "freq"), [("1h", "h"), ("4h", "4h")])
def test_misaligned_or_wrong_grid_candles_are_rejected(timeframe: str, freq: str) -> None:
    good = candles(np.full(48, 100.0), freq)
    assert check_quality(good, timeframe).ok
    shifted = good.copy()
    shifted.index = shifted.index + pd.Timedelta(minutes=7)
    assert any("aligned" in e for e in check_quality(shifted, timeframe).errors)
    daily = candles(np.full(10, 100.0), "D")
    assert check_quality(daily, timeframe).warnings  # a daily series has holes on an hourly grid
    assert check_quality(good.drop(good.index[5]), timeframe).missing_candles == 1


def test_annualised_figures_do_not_depend_on_the_timeframe() -> None:
    """The same price path sampled hourly, four-hourly and daily: Buy & Hold has
    the same CAGR, and volatility and Sharpe agree up to sampling noise."""
    rng = np.random.default_rng(3)
    hours = 24 * 730
    hourly_returns = rng.normal(0.0001, 0.006, hours)
    close = 100.0 * np.exp(np.cumsum(hourly_returns))
    hourly = candles(close, "h")
    rules = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    frames = {
        "1h": hourly,
        "4h": hourly.resample("4h").agg(rules),
        "1d": hourly.resample("D").agg(rules),
    }
    costs = CostConfig(fee_rate=0.0, slippage_bps=0.0)
    figures = {}
    for timeframe, frame in frames.items():
        equity = buy_and_hold({SYMBOL: frame}, 10_000.0, costs)
        figures[timeframe] = compute_metrics(equity, 10_000.0, periods=periods_per_year(timeframe))
        assert figures[timeframe]["bars"] == len(frame)
    assert figures["1h"]["total_return"] == pytest.approx(figures["1d"]["total_return"], rel=1e-9)
    for name, tolerance in (("cagr", 0.005), ("annual_volatility", 0.08), ("sharpe", 0.08)):
        values = [figures[t][name] for t in ("1h", "4h", "1d")]
        assert max(values) - min(values) <= tolerance * max(abs(v) for v in values) + 0.01, name
    # and the unannualised mistake is large: hourly returns treated as daily ones
    wrong = compute_metrics(buy_and_hold({SYMBOL: hourly}, 10_000.0, costs), 10_000.0)
    assert wrong["annual_volatility"] < figures["1h"]["annual_volatility"] / 4

    series = pd.Series(hourly_returns)
    assert sharpe_ratio(series, 8760) == pytest.approx(sharpe_ratio(series, 365) * math.sqrt(24))


def test_liquidity_columns_survive_normalisation_and_are_optional() -> None:
    frame = candles(np.full(5, 100.0), "D")
    assert list(normalize_ohlcv(frame).columns) == ["open", "high", "low", "close", "volume"]
    frame["quote_volume"], frame["trades"], frame["junk"] = 5e6, 1200.0, 1.0
    normalised = normalize_ohlcv(frame)
    assert list(normalised.columns) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
    ]
    assert check_quality(normalised).ok


def test_validation_pipeline_runs_on_four_hour_candles() -> None:
    rng = np.random.default_rng(7)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, 6 * 900)))
    data = {SYMBOL: candles(closes, "4h", start="2020-01-01")}
    cfg = config_for(
        "4h",
        validation={
            "walk_forward_train_days": 300,
            "walk_forward_test_days": 100,
            "monte_carlo_simulations": 200,
        },
    )
    report = run_validation(cfg, data, robustness=False)
    assert report.full.strategy["bars"] == len(closes)
    assert report.in_sample.end < report.out_of_sample.start
    assert report.out_of_sample.start - report.in_sample.end == pd.Timedelta(hours=4)
    assert report.walk_forward.windows
    assert report.monte_carlo.block_size == 20 * 6  # 20 days of four-hour candles
    assert report.provenance["timeframe"] == "4h"


def test_freqtrade_adapter_refuses_intraday_configurations() -> None:
    with pytest.raises(ConfigError, match="daily candles only"):
        FreqtradeBridge(config_for("4h"), TradingMode.PAPER)
    FreqtradeBridge(config_for("1d"), TradingMode.PAPER)
