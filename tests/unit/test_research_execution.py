"""Calibration fit, experiment ledger and the execution-cost study.

Synthetic, seeded data only: these tests check that the machinery is right
(fits recover known coefficients, the ledger only grows, every scenario is run
and recorded), never what the real strategy earns."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from qts.core.config import AppConfig
from qts.core.errors import DataError
from qts.research.calibration import by_liquidity, fit, walk_book
from qts.research.execution_study import (
    SCENARIOS,
    break_even,
    render,
    run_study,
    scenario_config,
)
from qts.research.ledger import ExperimentLedger


# ------------------------------------------------------------------ calibration
def test_walk_book_prices_an_order_against_the_levels() -> None:
    levels = [(100.0, 1.0), (101.0, 1.0), (102.0, 10.0)]
    assert walk_book(levels, 50.0, 100.0) == pytest.approx(0.0)
    # 100 at 100 and 101 at 101: average price (201) / (2 units) = 100.5 -> 50 bps
    assert walk_book(levels, 201.0, 100.0) == pytest.approx(50.0)
    assert math.isnan(walk_book(levels, 1e6, 100.0))  # the book cannot absorb it


def test_fit_recovers_a_known_spread_law_and_impact_coefficient() -> None:
    rng = np.random.default_rng(1)
    volume = np.exp(rng.uniform(np.log(1e5), np.log(1e10), 300))
    half = 700.0 * volume**-0.4 * np.exp(rng.normal(0, 0.1, 300))
    sigma = 0.05
    pairs = []
    for v, h in zip(volume, half, strict=True):
        row = {
            "symbol": "X",
            "quote_volume_24h": v,
            "half_spread_bps": h,
            "range_pct": sigma * 1.67,
        }
        for size in (1_000, 10_000, 100_000):
            extra = 0.4 * sigma * math.sqrt(size / v) * 1e4
            row[f"buy_{size}"] = row[f"sell_{size}"] = h + extra
        pairs.append(row)
    result = fit({"pairs": pairs})
    assert result["spread"]["exponent"] == pytest.approx(-0.4, abs=0.02)
    assert result["spread"]["coefficient_bps"] == pytest.approx(700.0, rel=0.2)
    assert result["impact"]["least_squares_k"] == pytest.approx(0.4, rel=1e-6)
    assert result["impact"]["k_quantiles"]["0.5"] == pytest.approx(0.4, rel=1e-6)
    assert sum(b["pairs"] for b in by_liquidity({"pairs": pairs})) == 300


def test_committed_snapshot_reproduces_the_default_coefficients() -> None:
    with open("research/calibration/binance-spot-2026-11-16.json", encoding="utf-8") as file:
        snapshot = json.load(file)
    result = fit(snapshot)
    defaults = AppConfig().model_dump()  # execution is off by default: compare to its class
    assert defaults["execution"] is None
    from qts.core.config import ExecutionConfig

    cfg = ExecutionConfig()
    assert result["spread"]["coefficient_bps"] == pytest.approx(
        cfg.half_spread_coefficient_bps, rel=0.01
    )
    assert result["spread"]["exponent"] == pytest.approx(cfg.half_spread_exponent, abs=0.001)
    # the high-cost scenarios are the snapshot's 90th percentiles, not invented
    assert result["spread"]["residual_multiplier"]["0.9"] == pytest.approx(3.3, abs=0.05)
    assert result["impact"]["k_quantiles"]["0.9"] == pytest.approx(1.81, abs=0.01)
    assert result["impact"]["least_squares_k"] == pytest.approx(6.55, abs=0.01)


# ----------------------------------------------------------------------- ledger
def test_ledger_only_appends_and_stamps_every_entry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = ExperimentLedger(tmp_path / "x" / "ledger.jsonl")
    first = ledger.append("data_access", segment="development")
    ledger.append("experiment", scenario="base", metrics={"cagr": 0.1})
    lines = (tmp_path / "x" / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == first["id"]
    assert {"at", "git_commit", "git_dirty", "kind"} <= set(first)
    assert [e["kind"] for e in ledger.entries()] == ["data_access", "experiment"]
    assert len(ledger.entries("experiment")) == 1
    with pytest.raises(DataError):
        ledger.append("anything_else")


# ------------------------------------------------------------------------ study
def _market(seed: int, n: int = 500) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.002, 0.03, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2018-01-01", periods=n, freq="D", tz="UTC", name="timestamp")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.01,
            "low": np.minimum(open_, close) * 0.99,
            "close": close,
            "volume": 1e8 / close,
            "quote_volume": 1e8,
        },
        index=index,
    )


def _hourly(daily: pd.DataFrame) -> pd.DataFrame:
    index = pd.date_range(
        daily.index[0],
        daily.index[-1] + pd.Timedelta(hours=23),
        freq="h",
        tz="UTC",
        name="timestamp",
    )
    opens = daily["open"].reindex(index, method="ffill") * 1.001  # later hours: a bit higher
    return pd.DataFrame(
        {
            "open": opens,
            "high": opens,
            "low": opens,
            "close": opens,
            "volume": 1.0,
            "quote_volume": 1e6,
        },
        index=index,
    )


def test_scenarios_change_only_costs_and_execution() -> None:
    base = AppConfig.model_validate(
        {"market": {"symbols": ["BTC/USDT"]}, "risk": {"allowed_symbols": ["BTC/USDT"]}}
    )
    for scenario in SCENARIOS:
        cfg = scenario_config(base, scenario, seed=3)
        assert cfg.strategy == base.strategy
        assert cfg.risk == base.risk
        assert cfg.portfolio == base.portfolio
        assert (cfg.execution is None) == (scenario.execution is None)
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))
    assert {
        "no_costs",
        "legacy",
        "base",
        "high",
        "stressed_slippage",
        "delay_1h",
        "delay_4h",
        "delay_1_candle",
        "rejections_10pct",
        "combined_worst",
    } <= set(names)


def test_break_even_interpolates_between_swept_points() -> None:
    sweep = [
        {"bps_per_fill": 0.0, "cagr": 0.2, "sharpe": 1.0},
        {"bps_per_fill": 100.0, "cagr": -0.2, "sharpe": 0.0},
    ]
    bench = {
        "exposure_matched_buy_and_hold": {"cagr": 0.1, "sharpe": 0.5},
        "buy_and_hold_equal_weight": {"cagr": 0.3, "sharpe": 2.0},
    }
    found = break_even(sweep, bench)
    assert found["cagr_zero_bps"] == pytest.approx(50.0)
    assert found["cagr_matched_benchmark_bps"] == pytest.approx(25.0)
    assert found["sharpe_buy_and_hold_bps"] == 0.0  # never beat it, even for free
    assert math.isnan(
        break_even([sweep[0], {**sweep[0], "bps_per_fill": 10.0}], bench)["cagr_zero_bps"]
    )


def test_study_runs_every_scenario_records_it_and_costs_never_help(tmp_path) -> None:  # type: ignore[no-untyped-def]
    symbols = ["BTC/USDT", "ETH/USDT"]
    config = AppConfig.model_validate(
        {
            "market": {"symbols": symbols},
            "risk": {"allowed_symbols": symbols},
            "strategy": {
                "params": {"breakout_window": 20, "exit_window": 10, "fast_ema": 10, "slow_ema": 30}
            },
        }
    )
    data = {"BTC/USDT": _market(1), "ETH/USDT": _market(2)}
    hourly = {s: _hourly(f) for s, f in data.items()}
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    report = run_study(config, data, hourly, "2018-03-01", "2019-05-01", ledger)

    by_name = {r["name"]: r["metrics"] for r in report["scenarios"]}
    assert set(by_name) == {s.name for s in SCENARIOS}
    assert by_name["no_costs"]["trades"] > 0
    assert by_name["no_costs"]["total_costs"] == 0.0
    # same signals, same fills: frictions can only take money away
    assert by_name["legacy"]["final_equity"] < by_name["no_costs"]["final_equity"]
    assert by_name["high"]["total_costs"] >= by_name["base"]["total_costs"]
    assert by_name["stressed_slippage"]["cost_spread"] > by_name["high"]["cost_spread"]
    draws = next(r for r in report["scenarios"] if r["name"] == "rejections_10pct")["draws"]
    assert draws["seeds"] == 20
    assert draws["cagr_min"] <= draws["cagr_median"] <= draws["cagr_max"]
    assert set(report["per_asset"]) == set(symbols)
    assert set(report["benchmarks"]) == {
        "cash",
        "buy_and_hold_equal_weight",
        "buy_and_hold_btc",
        "exposure_matched_buy_and_hold",
    }
    assert report["benchmarks"]["cash"]["cagr"] == 0.0
    sweep_cagr = [r["cagr"] for r in report["sweep"]]
    assert sweep_cagr == sorted(sweep_cagr, reverse=True)  # more cost, never more return
    # every run is in the ledger: scenarios, per-asset runs and sweep points
    experiments = ledger.entries("experiment")
    assert len(experiments) == len(SCENARIOS) + 2 * 7 + len(report["sweep"])
    assert all(e["segment"] == "development" and e["result_hash"] for e in experiments)
    text = render(report)
    assert "## Benchmarks" in text
    assert "Break-even" in text
