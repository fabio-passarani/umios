"""Robustness studies: structure, internal consistency, and that they are
diagnostics computed around the configuration — never a parameter search."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import run_backtest
from qts.backtesting.robustness import (
    REGIME_WINDOW,
    asset_correlation,
    cost_sensitivity,
    exposure_profile,
    parameter_sensitivity,
    regime_breakdown,
    regime_labels,
    sharpe_statistics,
    trade_significance,
    yearly_breakdown,
)
from qts.backtesting.validation import run_validation
from qts.core.config import AppConfig
from qts.reporting.report import render_markdown, validation_to_dict
from tests.conftest import make_candles, random_walk

SYMBOL = "TEST/USDT"


@pytest.fixture(scope="module")
def data() -> dict[str, pd.DataFrame]:
    return {SYMBOL: random_walk(1500, seed=7)}


def test_higher_costs_never_help(config: AppConfig, data: dict[str, pd.DataFrame]) -> None:
    rows = cost_sensitivity(config, data)
    base_fee, base_slip = config.costs.fee_rate, config.costs.slippage_bps
    assert any(r["fee_rate"] == base_fee and r["slippage_bps"] == base_slip for r in rows)
    assert all(r["trades"] > 0 for r in rows)
    by_fee = sorted(
        (r for r in rows if r["slippage_bps"] == base_slip), key=lambda r: r["fee_rate"]
    )
    by_slip = sorted(
        (r for r in rows if r["fee_rate"] == base_fee), key=lambda r: r["slippage_bps"]
    )
    for series in (by_fee, by_slip):
        returns = [r["total_return"] for r in series]
        assert len(returns) >= 5
        assert returns == sorted(returns, reverse=True)  # monotonic: more friction, less return
        assert returns[0] > returns[-1]


def test_parameter_sensitivity_moves_one_parameter_at_a_time(
    config: AppConfig, data: dict[str, pd.DataFrame]
) -> None:
    rows, summary = parameter_sensitivity(config, data)
    assert rows[0]["parameter"] == "(default)"
    defaults = config.strategy.params.model_dump()
    shocked = rows[1:]
    assert len(shocked) == summary["variants"] >= 10
    for row in shocked:
        assert row["value"] != defaults[row["parameter"]]
        assert 0.75 <= row["value"] / defaults[row["parameter"]] <= 1.25
    assert 0.0 <= summary["profitable_share"] <= 1.0
    assert summary["sharpe_min"] <= summary["sharpe_median"] <= summary["sharpe_max"]
    # the study must not alter the configuration it was given
    assert config.strategy.params.model_dump() == defaults


def test_regimes_use_only_the_past_and_partition_time(
    config: AppConfig, data: dict[str, pd.DataFrame]
) -> None:
    labels = regime_labels(data)
    assert (labels.iloc[:REGIME_WINDOW] == "warm-up").all()
    shorter = regime_labels({SYMBOL: data[SYMBOL].iloc[:900]})
    assert labels.iloc[:900].equals(shorter)  # no look-ahead: the future does not relabel the past

    result = run_backtest(config, data)
    bench = buy_and_hold(
        data, 10_000.0, config.costs, result.equity.index[0], result.equity.index[-1]
    )
    rows = regime_breakdown(result, bench, data)
    assert sum(r["days"] for r in rows) == len(result.equity)
    assert sum(r["share_of_time"] for r in rows) == pytest.approx(1.0)
    compounded = math.prod(1.0 + r["strategy_return"] for r in rows) - 1.0
    assert compounded == pytest.approx(result.equity.iloc[-1] / 10_000.0 - 1.0, rel=1e-9)
    assert {r["regime"] for r in rows} >= {"bull", "bear", "sideways"}

    up = {SYMBOL: make_candles(100.0 * 1.01 ** np.arange(400))}
    assert set(regime_labels(up).iloc[REGIME_WINDOW:]) == {"bull"}
    down = {SYMBOL: make_candles(100.0 * 0.99 ** np.arange(400))}
    assert set(regime_labels(down).iloc[REGIME_WINDOW:]) == {"bear"}
    flat = {SYMBOL: make_candles(np.full(400, 100.0))}
    assert set(regime_labels(flat).iloc[REGIME_WINDOW:]) == {"sideways"}


def test_yearly_breakdown_compounds_to_the_total(
    config: AppConfig, data: dict[str, pd.DataFrame]
) -> None:
    result = run_backtest(config, data)
    bench = buy_and_hold(
        data, 10_000.0, config.costs, result.equity.index[0], result.equity.index[-1]
    )
    rows = yearly_breakdown(result, bench)
    assert [r["year"] for r in rows] == sorted({ts.year for ts in result.equity.index})
    assert math.prod(1 + r["strategy_return"] for r in rows) - 1 == pytest.approx(
        result.equity.iloc[-1] / 10_000.0 - 1.0, rel=1e-9
    )
    assert math.prod(1 + r["benchmark_return"] for r in rows) - 1 == pytest.approx(
        bench.iloc[-1] / 10_000.0 - 1.0, rel=1e-9
    )
    assert sum(r["trades"] for r in rows) == len(result.trades)


def test_correlation_and_effective_bets() -> None:
    a = random_walk(800, seed=1)
    twin = {"A/USDT": a, "B/USDT": a.copy()}
    same = asset_correlation(twin)
    assert same["pairs"][0]["correlation"] == pytest.approx(1.0)
    assert same["effective_bets"] == pytest.approx(1.0, abs=1e-6)  # two names, one bet
    independent = asset_correlation({"A/USDT": a, "B/USDT": random_walk(800, seed=2)})
    assert abs(independent["pairs"][0]["correlation"]) < 0.2
    assert independent["effective_bets"] > 1.9
    assert asset_correlation({"A/USDT": a})["pairs"] == []


def test_exposure_profile_is_consistent(config: AppConfig, data: dict[str, pd.DataFrame]) -> None:
    result = run_backtest(config, data)
    profile = exposure_profile(result)
    assert profile["time_in_market"] == pytest.approx(float((result.exposure > 0).mean()))
    assert 0 < profile["average_exposure"] <= profile["average_exposure_when_invested"] <= 1.0
    assert profile["max_exposure"] <= 1.0 + 1e-9  # no leverage
    assert profile["by_symbol"][SYMBOL] == pytest.approx(profile["time_in_market"], abs=0.02)
    assert profile["longest_flat_days"] > 0


def test_significance_tools_say_no_on_noise_and_yes_on_an_edge() -> None:
    rng = np.random.default_rng(5)
    noise = pd.Series(rng.normal(0.0, 0.01, 2000))
    stats = sharpe_statistics(noise)
    assert stats["ci_low"] < 0 < stats["ci_high"]
    edge = pd.Series(rng.normal(0.002, 0.01, 2000))
    strong = sharpe_statistics(edge)
    assert strong["ci_low"] > 0
    assert strong["prob_positive"] > 0.99

    few = trade_significance([0.30, -0.05, -0.04, -0.06, 0.02], seed=1)
    assert few["trades"] == 5
    assert few["p_value"] > 0.05  # five trades prove nothing, however large one winner is
    many = trade_significance(list(rng.normal(0.02, 0.05, 400)), seed=1)
    assert many["p_value"] < 0.01
    assert trade_significance([], seed=1)["p_value"] == 1.0
    assert trade_significance([0.5], seed=1)["p_value"] == 1.0


def test_validation_report_contains_the_studies_and_honest_limits(
    config: AppConfig, data: dict[str, pd.DataFrame]
) -> None:
    report = run_validation(config, data)
    assert report.robustness is not None
    text = render_markdown(report)
    for heading in (
        "## Provenance",
        "## Cost sensitivity",
        "## Parameter sensitivity",
        "## Market regimes",
        "## Calendar years",
        "## Exposure and correlation",
        "## Statistical significance",
        "## Warnings and limits",
    ):
        assert heading in text
    assert "not pinned" in text  # this fixture has an open period: the report must say so
    assert any("moves when the data" in w for w in report.warnings)
    payload = validation_to_dict(report)
    assert payload["robustness"]["significance"]["full"]["trades"] == report.full.strategy["trades"]
    assert payload["provenance"]["result_hash"]
    # the validation must leave the strategy exactly as configured
    assert report.config["strategy"] == config.model_dump(mode="json")["strategy"]

    quick = run_validation(config, data, robustness=False)
    assert quick.robustness is None
    assert quick.full.strategy == report.full.strategy
