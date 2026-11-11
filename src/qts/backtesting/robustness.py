"""Robustness and significance studies.

These are *diagnostics*, not a search. Nothing here selects a parameter or a
cost assumption; every study reports what happens around the pre-declared
configuration so that a reader can judge how much of a result is structure and
how much is luck:

* cost sensitivity — does the edge survive worse fees and slippage?
* parameter sensitivity — is the default a lonely peak or part of a plateau?
* market regimes and calendar years — where does the result come from?
* asset correlation and exposure — how many independent bets are there really?
* statistical significance — could trades this good arise by chance?
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import BacktestResult, run_backtest
from qts.backtesting.metrics import PERIODS_PER_YEAR, compute_metrics, max_drawdown
from qts.core.config import AppConfig
from qts.market_data.schema import bars_per_day

#: Regime definition, fixed a priori: trailing 90-day return of the equal-weight
#: universe above +20% is a bull market, below -20% a bear market, else sideways.
REGIME_WINDOW = 90
REGIME_THRESHOLD = 0.20
FEE_MULTIPLES: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0)
SLIPPAGE_BPS: tuple[float, ...] = (0.0, 5.0, 10.0, 25.0, 50.0, 100.0)
PARAMETER_SHOCKS: tuple[float, ...] = (0.8, 1.2)
SHOCKED_PARAMETERS: tuple[str, ...] = (
    "breakout_window",
    "exit_window",
    "fast_ema",
    "slow_ema",
    "atr_window",
    "stop_atr_multiple",
    "max_atr_pct",
)


@dataclass
class RobustnessReport:
    cost_sensitivity: list[dict[str, float]]
    parameter_sensitivity: list[dict[str, Any]]
    parameter_summary: dict[str, float]
    regimes: list[dict[str, Any]]
    yearly: list[dict[str, Any]]
    correlation: dict[str, Any]
    exposure: dict[str, Any]
    significance: dict[str, Any]
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------ helpers
def _row(result: BacktestResult, capital: float, periods: int) -> dict[str, float]:
    m = compute_metrics(result.equity, capital, result.trades, result.exposure, periods)
    return {
        "total_return": m["total_return"],
        "cagr": m["cagr"],
        "sharpe": m["sharpe"],
        "max_drawdown": m["max_drawdown"],
        "trades": m["trades"],
    }


def _with(config: AppConfig, section: str, **changes: Any) -> AppConfig | None:
    raw = config.model_dump()
    target = raw["strategy"]["params"] if section == "params" else raw[section]
    target.update(changes)
    try:
        return AppConfig.model_validate(raw)
    except ValueError:
        return None  # the shocked value violates a constraint (e.g. fast_ema >= slow_ema)


def _compound(returns: pd.Series) -> float:
    return float(np.prod(1.0 + returns.to_numpy(dtype=float)) - 1.0)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ------------------------------------------------------------------------- costs
def cost_sensitivity(
    config: AppConfig, data: dict[str, pd.DataFrame], signals: dict[str, pd.DataFrame] | None = None
) -> list[dict[str, float]]:
    """Re-run the unchanged strategy under harsher (and kinder) frictions."""
    capital = config.portfolio.initial_capital
    base_fee, base_slip = config.costs.fee_rate, config.costs.slippage_bps
    scenarios = [(base_fee * k, base_slip) for k in FEE_MULTIPLES]
    scenarios += [(base_fee, bps) for bps in SLIPPAGE_BPS if bps != base_slip]
    scenarios.append((base_fee * 2.0, 25.0))  # both worse at once
    rows: list[dict[str, float]] = []
    for fee, slippage in scenarios:
        cfg = _with(config, "costs", fee_rate=fee, slippage_bps=slippage)
        if cfg is None:
            continue
        result = run_backtest(cfg, data, signals=signals)
        rows.append(
            {
                "fee_rate": fee,
                "slippage_bps": slippage,
                **_row(result, capital, config.periods_per_year),
            }
        )
    return rows


# -------------------------------------------------------------------- parameters
def parameter_sensitivity(
    config: AppConfig, data: dict[str, pd.DataFrame]
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Move one parameter at a time by ±20% and look at the neighbourhood."""
    capital = config.portfolio.initial_capital
    params = config.strategy.params.model_dump()
    base = _row(run_backtest(config, data), capital, config.periods_per_year)
    rows: list[dict[str, Any]] = [{"parameter": "(default)", "value": None, **base}]
    for name in SHOCKED_PARAMETERS:
        for shock in PARAMETER_SHOCKS:
            original = params[name]
            value: float | int = (
                max(2, round(original * shock)) if isinstance(original, int) else original * shock
            )
            if value == original:
                continue
            cfg = _with(config, "params", **{name: value})
            if cfg is None:
                continue
            rows.append(
                {
                    "parameter": name,
                    "value": value,
                    **_row(run_backtest(cfg, data), capital, config.periods_per_year),
                }
            )
    shocked = rows[1:]
    sharpes = np.array([r["sharpe"] for r in shocked]) if shocked else np.array([base["sharpe"]])
    returns = np.array([r["total_return"] for r in shocked]) if shocked else np.array([0.0])
    summary = {
        "variants": float(len(shocked)),
        "profitable_share": float((returns > 0).mean()),
        "sharpe_min": float(sharpes.min()),
        "sharpe_median": float(np.median(sharpes)),
        "sharpe_max": float(sharpes.max()),
        "default_sharpe": base["sharpe"],
        # share of neighbours that do better than the default: ~0.5 means "nothing special"
        "better_than_default_share": float((sharpes > base["sharpe"]).mean()),
        "return_min": float(returns.min()),
        "return_max": float(returns.max()),
    }
    return rows, summary


# ----------------------------------------------------------------------- regimes
def market_index(data: dict[str, pd.DataFrame]) -> pd.Series:
    """Equal-weight per-candle return of the universe (assets not yet listed are skipped)."""
    closes = pd.concat({s: f["close"] for s, f in data.items()}, axis=1).sort_index()
    return closes.pct_change().mean(axis=1, skipna=True).fillna(0.0)


def regime_labels(data: dict[str, pd.DataFrame], bars_in_a_day: int = 1) -> pd.Series:
    """Label each candle bull / bear / sideways from the *trailing* window (no
    look-ahead). The window is :data:`REGIME_WINDOW` days whatever the timeframe."""
    growth = (1.0 + market_index(data)).cumprod()
    trailing = growth / growth.shift(REGIME_WINDOW * bars_in_a_day) - 1.0
    labels = pd.Series("sideways", index=growth.index)
    labels[trailing > REGIME_THRESHOLD] = "bull"
    labels[trailing < -REGIME_THRESHOLD] = "bear"
    labels[trailing.isna()] = "warm-up"
    return labels


def regime_breakdown(
    result: BacktestResult,
    benchmark: pd.Series,
    data: dict[str, pd.DataFrame],
    bars_in_a_day: int = 1,
) -> list[dict[str, Any]]:
    labels = regime_labels(data, bars_in_a_day).reindex(result.equity.index).fillna("warm-up")
    strategy = result.returns
    bench = benchmark.reindex(result.equity.index).ffill()
    bench_returns = bench.pct_change()
    bench_returns.iloc[0] = bench.iloc[0] / result.initial_capital - 1.0
    in_market = result.exposure > 0
    rows: list[dict[str, Any]] = []
    for regime in ("bull", "sideways", "bear", "warm-up"):
        mask = labels == regime
        days = int(mask.sum())
        if days == 0:
            continue
        rows.append(
            {
                "regime": regime,
                "days": days,
                "share_of_time": days / len(labels),
                "strategy_return": _compound(strategy[mask]),
                "benchmark_return": _compound(bench_returns[mask]),
                "time_in_market": float(in_market[mask].mean()),
            }
        )
    return rows


def yearly_breakdown(result: BacktestResult, benchmark: pd.Series) -> list[dict[str, Any]]:
    bench = benchmark.reindex(result.equity.index).ffill()
    bench_returns = bench.pct_change()
    bench_returns.iloc[0] = bench.iloc[0] / result.initial_capital - 1.0
    strategy = result.returns
    years = pd.DatetimeIndex(result.equity.index).year
    closed = pd.Series([pd.Timestamp(t.exit_time).year for t in result.trades], dtype="int64")
    rows = []
    for year in sorted(set(years)):
        mask = years == year
        rows.append(
            {
                "year": int(year),
                "strategy_return": _compound(strategy[mask]),
                "benchmark_return": _compound(bench_returns[mask]),
                "max_drawdown": max(0.0, max_drawdown(result.equity[mask])),
                "trades": int((closed == year).sum()),
            }
        )
    return rows


# ------------------------------------------------------- correlation and exposure
def asset_correlation(data: dict[str, pd.DataFrame], window: int = 90) -> dict[str, Any]:
    returns = pd.concat({s: f["close"].pct_change() for s, f in data.items()}, axis=1).dropna()
    symbols = list(returns.columns)
    if len(symbols) < 2 or len(returns) < window:
        return {"pairs": [], "effective_bets": float(len(symbols)), "window": window}
    pairs = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1 :]:
            rolling = returns[a].rolling(window).corr(returns[b]).dropna()
            pairs.append(
                {
                    "pair": f"{a} ~ {b}",
                    "correlation": float(returns[a].corr(returns[b])),
                    "rolling_min": float(rolling.min()),
                    "rolling_median": float(rolling.median()),
                    "rolling_max": float(rolling.max()),
                }
            )
    # "effective number of bets" from the correlation matrix eigenvalues
    eigen = np.linalg.eigvalsh(returns.corr().to_numpy())
    weights = eigen[eigen > 0] / eigen[eigen > 0].sum()
    effective = float(math.exp(-(weights * np.log(weights)).sum()))
    return {"pairs": pairs, "effective_bets": effective, "window": window}


def exposure_profile(result: BacktestResult) -> dict[str, Any]:
    share = (result.exposure / result.equity).clip(lower=0.0)
    index = result.equity.index
    held = pd.DataFrame(False, index=index, columns=sorted({t.symbol for t in result.trades}))
    for trade in result.trades:
        held.loc[pd.Timestamp(trade.entry_time) : pd.Timestamp(trade.exit_time), trade.symbol] = (
            True
        )
    for position in result.open_positions:
        held.loc[pd.Timestamp(position.entry_time) :, position.symbol] = True
    holding = [(pd.Timestamp(t.exit_time) - pd.Timestamp(t.entry_time)).days for t in result.trades]
    return {
        "time_in_market": float((result.exposure > 0).mean()),
        "average_exposure": float(share.mean()),
        "average_exposure_when_invested": float(share[share > 0].mean())
        if (share > 0).any()
        else 0.0,
        "max_exposure": float(share.max()),
        "average_positions": float(held.sum(axis=1).mean()) if len(held.columns) else 0.0,
        "time_all_assets_held": float(held.all(axis=1).mean()) if len(held.columns) > 1 else 0.0,
        "by_symbol": {s: float(held[s].mean()) for s in held.columns},
        "median_holding_days": float(np.median(holding)) if holding else 0.0,
        "longest_flat_days": _longest_run(~(result.exposure > 0).to_numpy()),
    }


def _longest_run(flags: np.ndarray[Any, Any]) -> int:
    longest = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


# ------------------------------------------------------------------ significance
def sharpe_statistics(returns: pd.Series, periods: int = PERIODS_PER_YEAR) -> dict[str, float]:
    """Annualised Sharpe with its standard error (Lo, 2002, adjusted for skew and
    kurtosis as in Bailey & López de Prado) and the probability that the true
    Sharpe is above zero."""
    values = returns.to_numpy(dtype=float)
    n = len(values)
    std = float(values.std(ddof=1)) if n > 2 else 0.0
    if std == 0.0:
        return {
            "sharpe": 0.0,
            "std_error": 0.0,
            "ci_low": 0.0,
            "ci_high": 0.0,
            "prob_positive": 0.5,
        }
    daily = float(values.mean()) / std
    centred = (values - values.mean()) / std
    skew, kurtosis = float((centred**3).mean()), float((centred**4).mean())
    variance = (1.0 - skew * daily + (kurtosis - 1.0) / 4.0 * daily**2) / (n - 1)
    error = math.sqrt(max(variance, 0.0))
    scale = math.sqrt(periods)
    return {
        "sharpe": daily * scale,
        "std_error": error * scale,
        "ci_low": (daily - 1.96 * error) * scale,
        "ci_high": (daily + 1.96 * error) * scale,
        "prob_positive": _phi(daily / error) if error > 0 else 0.5,
    }


def trade_significance(
    trade_returns: list[float], seed: int, draws: int = 20_000
) -> dict[str, float]:
    """Is the mean trade return distinguishable from zero?

    One-sided bootstrap test under the null "the expected trade return is zero"
    (the sample is re-centred on zero and resampled). With few trades the
    answer is usually "no", whatever the backtest's total return says.
    """
    values = np.asarray(trade_returns, dtype=float)
    n = len(values)
    if n < 2:
        return {
            "trades": float(n),
            "mean_return": float(values.mean()) if n else 0.0,
            "t_stat": 0.0,
            "p_value": 1.0,
        }
    mean, std = float(values.mean()), float(values.std(ddof=1))
    rng = np.random.default_rng(seed)
    centred = values - mean
    resampled = rng.choice(centred, size=(draws, n), replace=True).mean(axis=1)
    return {
        "trades": float(n),
        "mean_return": mean,
        "t_stat": mean / (std / math.sqrt(n)) if std > 0 else 0.0,
        "p_value": float((resampled >= mean).mean()),
    }


def significance(
    config: AppConfig, full: BacktestResult, out_of_sample: BacktestResult
) -> dict[str, Any]:
    seed = config.validation.seed
    trials = int(np.prod([len(v) for v in config.validation.parameter_grid.values()] or [1]))
    return {
        "full": {
            **sharpe_statistics(full.returns, config.periods_per_year),
            **trade_significance([t.return_pct for t in full.trades], seed),
        },
        "out_of_sample": {
            **sharpe_statistics(out_of_sample.returns, config.periods_per_year),
            **trade_significance([t.return_pct for t in out_of_sample.trades], seed),
        },
        "declared_trials": trials,
    }


# ---------------------------------------------------------------------- pipeline
def run_robustness(
    config: AppConfig,
    data: dict[str, pd.DataFrame],
    full: BacktestResult,
    out_of_sample: BacktestResult,
) -> RobustnessReport:
    capital = config.portfolio.initial_capital
    benchmark = buy_and_hold(
        data, capital, config.costs, full.equity.index[0], full.equity.index[-1]
    )
    parameters, summary = parameter_sensitivity(config, data)
    stats = significance(config, full, out_of_sample)
    correlation = asset_correlation(data, 90 * bars_per_day(config.market.timeframe))
    exposure = exposure_profile(full)

    notes: list[str] = []
    oos = stats["out_of_sample"]
    if oos["trades"] < 30:
        notes.append(
            f"Out of sample there are {int(oos['trades'])} closed trades: far too few for any "
            "statistical claim. The out-of-sample result is an observation, not a validation."
        )
    if oos["ci_low"] < 0 < oos["ci_high"]:
        notes.append(
            f"The 95% interval of the out-of-sample Sharpe ratio is [{oos['ci_low']:.2f}, "
            f"{oos['ci_high']:.2f}]: it includes zero."
        )
    if stats["full"]["p_value"] > 0.05:
        notes.append(
            f"Full period: the mean trade return is not significantly above zero "
            f"(bootstrap p = {stats['full']['p_value']:.2f})."
        )
    if correlation["pairs"] and correlation["effective_bets"] < len(data) * 0.75:
        notes.append(
            f"The {len(data)} assets behave like {correlation['effective_bets']:.1f} independent "
            "bets: positions in both are largely the same position twice."
        )
    if summary["variants"] and summary["profitable_share"] < 1.0:
        notes.append(
            f"{1 - summary['profitable_share']:.0%} of the ±20% parameter variants lose money."
        )
    costs = cost_sensitivity(config, data)
    harsh = [r for r in costs if r["total_return"] <= 0]
    if harsh:
        worst = min(harsh, key=lambda r: (r["fee_rate"], r["slippage_bps"]))
        notes.append(
            f"The result turns negative at fee {worst['fee_rate']:.2%} / slippage "
            f"{worst['slippage_bps']:.0f} bps."
        )
    notes.append(
        f"{stats['declared_trials']} parameter combinations were declared for walk-forward "
        "selection; any figure chosen as the best of several is biased upwards."
    )
    notes.append(
        "Regimes are defined on the same two assets that are traded, so 'bull' and 'bear' are "
        "descriptions of this sample, not independent market states."
    )
    return RobustnessReport(
        cost_sensitivity=costs,
        parameter_sensitivity=parameters,
        parameter_summary=summary,
        regimes=regime_breakdown(full, benchmark, data, bars_per_day(config.market.timeframe)),
        yearly=yearly_breakdown(full, benchmark),
        correlation=correlation,
        exposure=exposure,
        significance=stats,
        notes=notes,
    )
