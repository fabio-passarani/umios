"""Validation pipeline: in/out-of-sample split, walk-forward analysis, Monte Carlo.

A good-looking backtest proves very little. Each tool here attacks a specific
way of fooling oneself:

* **Out-of-sample** — performance on data never used to choose anything.
* **Walk-forward** — repeatedly choose parameters on a train window, then trade
  the *next*, unseen window; only the stitched out-of-sample segments count.
* **Monte Carlo** — resample the realised daily returns to see how much of the
  result (and of the drawdown) could be luck of ordering.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import BacktestResult, provenance_of, run_backtest
from qts.backtesting.metrics import compute_metrics, max_drawdown, sharpe_ratio
from qts.backtesting.robustness import RobustnessReport, run_robustness
from qts.core.config import AppConfig
from qts.core.errors import DataError
from qts.core.types import Trade
from qts.strategies import TrendFollowingStrategy


# ------------------------------------------------------------- in / out of sample
def split_point(
    index: pd.DatetimeIndex, out_of_sample_fraction: float, out_of_sample_start: str | None = None
) -> pd.Timestamp:
    """First timestamp of the out-of-sample segment (chronological split, no shuffle).

    A configured ``out_of_sample_start`` wins: the boundary is then a fixed
    date. The fraction is the fallback and moves whenever the data grows.
    """
    if len(index) < 10:
        raise DataError("not enough bars to split in/out of sample")
    if out_of_sample_start is not None:
        boundary = pd.Timestamp(out_of_sample_start, tz="UTC")
        after = index[index >= boundary]
        if len(after) == 0 or len(after) == len(index):
            raise DataError(
                f"out_of_sample_start {out_of_sample_start} leaves one of the two samples empty"
            )
        return pd.Timestamp(after[0])
    position = int(len(index) * (1.0 - out_of_sample_fraction))
    return index[min(max(position, 1), len(index) - 1)]


def common_index(data: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """Union timeline of all symbols."""
    return pd.DatetimeIndex(sorted(set().union(*(frame.index for frame in data.values()))))


@dataclass
class SampleReport:
    """Strategy vs. benchmark on one period."""

    label: str
    start: pd.Timestamp
    end: pd.Timestamp
    strategy: dict[str, float]
    benchmark: dict[str, float]
    result: BacktestResult
    benchmark_equity: pd.Series


def evaluate_period(
    config: AppConfig,
    data: dict[str, pd.DataFrame],
    label: str,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> SampleReport:
    """Backtest one period and compare with Buy & Hold over the same bars."""
    result = run_backtest(config, data, start=start, end=end)
    first, last = result.equity.index[0], result.equity.index[-1]
    capital = config.portfolio.initial_capital
    bench = buy_and_hold(data, capital, config.costs, first, last)
    return SampleReport(
        label=label,
        start=first,
        end=last,
        strategy=compute_metrics(result.equity, capital, result.trades, result.exposure),
        benchmark=compute_metrics(bench, capital),
        result=result,
        benchmark_equity=bench,
    )


# ------------------------------------------------------------------ walk-forward
@dataclass
class WalkForwardWindow:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    chosen_params: dict[str, float]
    train_sharpe: float
    test_sharpe: float
    test_return: float
    test_trades: int


@dataclass
class WalkForwardReport:
    windows: list[WalkForwardWindow]
    oos_returns: pd.Series
    oos_metrics: dict[str, float]
    benchmark_metrics: dict[str, float]
    efficiency: float
    grid: dict[str, tuple[float, ...]] = field(default_factory=dict)


def _with_params(config: AppConfig, overrides: dict[str, float]) -> AppConfig:
    params = config.strategy.params.model_dump()
    for key, value in overrides.items():
        if key not in params:
            raise DataError(f"parameter_grid key {key!r} is not a strategy parameter")
        params[key] = type(params[key])(value)
    raw = config.model_dump()
    raw["strategy"]["params"] = params
    return AppConfig.model_validate(raw)


def walk_forward(config: AppConfig, data: dict[str, pd.DataFrame]) -> WalkForwardReport:
    """Rolling walk-forward analysis over the configured parameter grid.

    For every window the parameter set with the best *train* Sharpe is traded on
    the following *test* window. Each test window starts flat with the initial
    capital; the windows' daily returns are chained into one out-of-sample track.
    """
    v = config.validation
    index = common_index(data)
    train, test = (
        pd.Timedelta(days=v.walk_forward_train_days),
        pd.Timedelta(days=v.walk_forward_test_days),
    )
    keys = sorted(v.parameter_grid)
    combos = [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(v.parameter_grid[k] for k in keys))
    ] or [{}]
    candidates = [(combo, _with_params(config, combo)) for combo in combos]
    # Signals depend only on parameters and (causal) data: compute once per candidate.
    signal_cache = [
        {
            s: TrendFollowingStrategy(cfg.strategy.params).generate_signals(f)
            for s, f in data.items()
        }
        for _combo, cfg in candidates
    ]

    windows: list[WalkForwardWindow] = []
    pieces: list[pd.Series] = []
    train_sharpes: list[float] = []
    oos_trades: list[Trade] = []
    cursor = index[0]
    one_bar = pd.Timedelta(days=1)
    while cursor + train + one_bar <= index[-1]:
        train_start, train_end = cursor, cursor + train - one_bar
        test_start = cursor + train
        test_end = min(test_start + test - one_bar, index[-1])
        best: tuple[float, int] | None = None
        for i, (_combo, cfg) in enumerate(candidates):
            run = run_backtest(cfg, data, start=train_start, end=train_end, signals=signal_cache[i])
            score = sharpe_ratio(run.returns)
            if best is None or score > best[0]:
                best = (score, i)
        assert best is not None
        chosen = best[1]
        oos = run_backtest(
            candidates[chosen][1],
            data,
            start=test_start,
            end=test_end,
            signals=signal_cache[chosen],
        )
        windows.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                test_start=oos.equity.index[0],
                test_end=oos.equity.index[-1],
                chosen_params=candidates[chosen][0],
                train_sharpe=best[0],
                test_sharpe=sharpe_ratio(oos.returns),
                test_return=float(oos.equity.iloc[-1] / oos.initial_capital - 1.0),
                test_trades=len(oos.trades),
            )
        )
        train_sharpes.append(best[0])
        pieces.append(oos.returns)
        oos_trades.extend(oos.trades)
        cursor = cursor + test
    if not windows:
        raise DataError("not enough history for a single walk-forward window")

    oos_returns = pd.concat(pieces).sort_index()
    capital = config.portfolio.initial_capital
    oos_equity = capital * (1.0 + oos_returns).cumprod()
    bench = buy_and_hold(data, capital, config.costs, oos_returns.index[0], oos_returns.index[-1])
    oos_metrics = compute_metrics(oos_equity, capital, oos_trades)
    mean_train = float(np.mean(train_sharpes))
    efficiency = oos_metrics["sharpe"] / mean_train if mean_train > 0 else 0.0
    return WalkForwardReport(
        windows=windows,
        oos_returns=oos_returns,
        oos_metrics=oos_metrics,
        benchmark_metrics=compute_metrics(bench, capital),
        efficiency=float(efficiency),
        grid=dict(v.parameter_grid),
    )


# -------------------------------------------------------------------- Monte Carlo
@dataclass
class MonteCarloReport:
    simulations: int
    block_size: int
    seed: int
    total_return: dict[str, float]
    max_drawdown: dict[str, float]
    sharpe: dict[str, float]
    probability_of_loss: float
    observed: dict[str, float]


def _percentiles(values: np.ndarray[Any, Any]) -> dict[str, float]:
    points = np.percentile(values, [5, 25, 50, 75, 95])
    return {f"p{p:02d}": float(x) for p, x in zip((5, 25, 50, 75, 95), points, strict=True)}


def monte_carlo(
    returns: pd.Series, simulations: int, seed: int, block_size: int = 20
) -> MonteCarloReport:
    """Stationary block bootstrap of daily returns.

    Blocks (rather than single days) are resampled so volatility clustering and
    short-term autocorrelation survive. The seed makes the study reproducible.
    """
    values = returns.to_numpy(dtype=float)
    n = len(values)
    if n < block_size * 2:
        raise DataError("not enough returns for a Monte Carlo study")
    rng = np.random.default_rng(seed)
    blocks = int(np.ceil(n / block_size))
    starts = rng.integers(0, n, size=(simulations, blocks))
    offsets = np.arange(block_size)
    idx = ((starts[:, :, None] + offsets[None, None, :]) % n).reshape(simulations, -1)[:, :n]
    paths = np.cumprod(1.0 + values[idx], axis=1)
    totals = paths[:, -1] - 1.0
    peaks = np.maximum.accumulate(np.maximum(paths, 1.0), axis=1)
    drawdowns = (1.0 - paths / peaks).max(axis=1)
    sampled = values[idx]
    std = sampled.std(axis=1, ddof=1)
    sharpes = np.divide(sampled.mean(axis=1), std, out=np.zeros(simulations), where=std > 0)
    sharpes = sharpes * np.sqrt(365)
    equity = (1.0 + returns).cumprod()
    return MonteCarloReport(
        simulations=simulations,
        block_size=block_size,
        seed=seed,
        total_return=_percentiles(totals),
        max_drawdown=_percentiles(drawdowns),
        sharpe=_percentiles(sharpes),
        probability_of_loss=float((totals < 0).mean()),
        observed={
            "total_return": float(equity.iloc[-1] - 1.0),
            "max_drawdown": max_drawdown(pd.concat([pd.Series([1.0]), equity], ignore_index=True)),
            "sharpe": sharpe_ratio(returns),
        },
    )


# ----------------------------------------------------------------------- pipeline
@dataclass
class ValidationReport:
    """Bundle of every validation study for one configuration."""

    config_fingerprint: str
    config: dict[str, Any]
    full: SampleReport
    in_sample: SampleReport
    out_of_sample: SampleReport
    walk_forward: WalkForwardReport
    monte_carlo: MonteCarloReport
    warnings: list[str]
    split: pd.Timestamp | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    robustness: RobustnessReport | None = None


def run_validation(
    config: AppConfig, data: dict[str, pd.DataFrame], robustness: bool = True
) -> ValidationReport:
    """Run the whole pipeline and attach plain-language warnings."""
    index = common_index(data)
    split = split_point(
        index, config.validation.out_of_sample_fraction, config.validation.out_of_sample_start
    )
    full = evaluate_period(config, data, "full")
    in_sample = evaluate_period(config, data, "in_sample", end=split - pd.Timedelta(days=1))
    out_sample = evaluate_period(config, data, "out_of_sample", start=split)
    wf = walk_forward(config, data)
    mc = monte_carlo(
        full.result.returns, config.validation.monte_carlo_simulations, config.validation.seed
    )

    warnings: list[str] = [
        "Universe was chosen with hindsight (assets that still exist today): results carry "
        "survivorship bias and overstate what was knowable in advance.",
    ]
    if full.strategy["trades"] < 30:
        warnings.append(
            f"Only {int(full.strategy['trades'])} closed trades: too few for statistically "
            "meaningful trade metrics."
        )
    if out_sample.strategy["sharpe"] < 0.5 * in_sample.strategy["sharpe"]:
        warnings.append("Out-of-sample Sharpe is less than half the in-sample Sharpe.")
    if full.strategy["total_return"] < full.benchmark["total_return"]:
        warnings.append("Strategy total return is below Buy & Hold over the full period.")
    if wf.efficiency < 0.5:
        warnings.append(
            f"Walk-forward efficiency is {wf.efficiency:.2f} (<0.5): parameters chosen in-sample "
            "do not carry over well."
        )
    if mc.probability_of_loss > 0.2:
        warnings.append(f"Monte Carlo: {mc.probability_of_loss:.0%} of resampled paths lose money.")
    if full.result.kill_switch_active:
        warnings.append("The kill switch tripped during the full-period backtest.")
    provenance = provenance_of(full.result, data)
    if not provenance["period"]["pinned"]:
        warnings.append(
            "data.end is not set: the period grows every day, so this report cannot be "
            "reproduced later. Pin data.end before publishing a result."
        )
    if config.validation.out_of_sample_start is None:
        warnings.append(
            "The in/out-of-sample boundary is a fraction of the data and moves when the data "
            "grows. Set validation.out_of_sample_start to fix it."
        )
    study = run_robustness(config, data, full.result, out_sample.result) if robustness else None
    if study is not None:
        warnings.extend(study.notes)
    return ValidationReport(
        config_fingerprint=config.fingerprint(),
        config=config.model_dump(mode="json"),
        full=full,
        in_sample=in_sample,
        out_of_sample=out_sample,
        walk_forward=wf,
        monte_carlo=mc,
        warnings=warnings,
        split=split,
        provenance=provenance,
        robustness=study,
    )
