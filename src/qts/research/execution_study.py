"""Does the existing strategy's result survive realistic execution?

A diagnostic, not a search. The strategy, its parameters and every scenario
below are fixed *before* the study is run; nothing here selects anything. The
study re-runs the unchanged strategy under a list of execution scenarios and
reports, net of costs, what is left of the result:

* reference points — no costs at all, and the fixed-cost model every result
  published so far used;
* cost levels — base estimate, high costs, stressed slippage (taken from the
  dispersion observed in the calibration snapshot, not invented);
* liquidity — the same market 10, 100 and 1000 times thinner (equivalently: an
  account 10, 100 and 1000 times larger);
* timing — orders that reach the market 1 hour, 4 hours or one whole candle late
  (hour delays use the open of real hourly candles);
* the venue — orders refused at random (repeated over many seeds: one seed would
  be a result chosen by luck);
* everything bad at once.

Only the **development** segment of the sealed dataset is read. Every run is
appended to the experiment ledger.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import BacktestResult, compute_signal_frames, run_backtest
from qts.backtesting.metrics import compute_metrics, max_drawdown
from qts.backtesting.robustness import sharpe_statistics, trade_significance
from qts.core.config import AppConfig
from qts.core.provenance import data_version, result_hash
from qts.core.types import OrderReason
from qts.research.ledger import ExperimentLedger
from qts.strategies import build_strategy

STUDY = "m2-execution-costs"


@dataclass(frozen=True)
class Scenario:
    """One way the market could treat the strategy's orders.

    ``execution`` is ``None`` for the legacy fixed-cost model; otherwise the
    overrides applied to the realistic model's defaults. ``basis`` says where
    the numbers come from.
    """

    name: str
    group: str
    description: str
    basis: str
    costs: dict[str, float] = field(default_factory=dict)
    execution: dict[str, Any] | None = None
    seeds: int = 1

    @property
    def needs_hourly(self) -> bool:
        return bool(self.execution and self.execution.get("delay_hours"))


#: Fixed before the study is run. Multipliers come from the calibration
#: snapshot: 3.3 is the 90th percentile of observed spreads around the fitted
#: curve; 1.81 the 90th percentile of the impact coefficient across sampled
#: books; 6.55 the least-squares coefficient, dominated by the thinnest books.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "no_costs",
        "reference",
        "No costs at all",
        "theoretical: what the signals are worth before anybody is paid",
        costs={"fee_rate": 0.0, "slippage_bps": 0.0},
    ),
    Scenario(
        "legacy",
        "reference",
        "Published model: 0.10% fee + fixed 5 bps slippage, always filled",
        "fee observed, slippage assumed",
    ),
    Scenario(
        "base",
        "costs",
        "Base estimate: spread and impact from liquidity and volatility",
        "fee observed; spread and impact estimated (snapshot fit); participation assumed 1%",
        execution={},
    ),
    Scenario(
        "high",
        "costs",
        "High costs: spread x3.3, impact coefficient 1.81",
        "90th percentiles of the observed dispersion",
        execution={"spread_multiplier": 3.3, "impact_coefficient": 1.81},
    ),
    Scenario(
        "stressed_slippage",
        "costs",
        "Stressed slippage: spread x10, impact coefficient 6.55",
        "assumed stress beyond the observed 90th percentile",
        execution={"spread_multiplier": 10.0, "impact_coefficient": 6.55},
    ),
    Scenario(
        "liquidity_10x_thinner",
        "liquidity",
        "Market 10x thinner (or account 10x larger)",
        "assumed scenario",
        execution={"liquidity_scale": 0.1},
    ),
    Scenario(
        "liquidity_100x_thinner",
        "liquidity",
        "Market 100x thinner (or account 100x larger)",
        "assumed scenario",
        execution={"liquidity_scale": 0.01},
    ),
    Scenario(
        "liquidity_1000x_thinner",
        "liquidity",
        "Market 1000x thinner (or account 1000x larger)",
        "assumed scenario",
        execution={"liquidity_scale": 0.001},
    ),
    Scenario(
        "delay_1h",
        "timing",
        "Orders reach the market 1 hour after the candle opens",
        "fill price observed: open of the hourly candle",
        execution={"delay_hours": 1},
    ),
    Scenario(
        "delay_4h",
        "timing",
        "Orders reach the market 4 hours after the candle opens",
        "fill price observed: open of the hourly candle",
        execution={"delay_hours": 4},
    ),
    Scenario(
        "delay_1_candle",
        "timing",
        "Orders reach the market one whole candle (1 day) late",
        "fill price observed: open of the next candle",
        execution={"delay_bars": 1},
    ),
    Scenario(
        "rejections_10pct",
        "venue",
        "10% of orders refused by the venue (20 random draws)",
        "assumed rate; entries are not chased, exits are retried next candle",
        execution={"rejection_rate": 0.10},
        seeds=20,
    ),
    Scenario(
        "combined_worst",
        "combined",
        "High costs + 10x thinner + 4h delay + 10% rejections (20 draws)",
        "all the above at once",
        execution={
            "spread_multiplier": 3.3,
            "impact_coefficient": 1.81,
            "liquidity_scale": 0.1,
            "delay_hours": 4,
            "rejection_rate": 0.10,
        },
        seeds=20,
    ),
)

#: Scenarios repeated for each asset alone and per calendar year.
KEY_SCENARIOS: tuple[str, ...] = (
    "no_costs",
    "legacy",
    "base",
    "high",
    "stressed_slippage",
    "liquidity_100x_thinner",
    "delay_4h",
)
#: Uniform cost per fill (bps, fee included) for the break-even sweep.
SWEEP_BPS: tuple[float, ...] = (0, 10, 15, 25, 50, 75, 100, 150, 200, 300, 400)

REPORTED = (
    "cagr",
    "total_return",
    "sharpe",
    "sortino",
    "calmar",
    "max_drawdown",
    "profit_factor",
    "win_rate",
    "expectancy",
    "turnover",
    "trades",
    "total_costs",
    "cost_fees",
    "cost_spread",
    "cost_impact",
    "cost_drag",
    "cost_per_traded_bps",
    "average_exposure",
    "time_in_market",
    "final_equity",
)


def scenario_config(config: AppConfig, scenario: Scenario, seed: int | None = None) -> AppConfig:
    """The configuration of ``scenario``: only costs and execution differ."""
    raw = config.model_dump()
    raw["costs"].update(scenario.costs)
    if scenario.execution is None:
        raw["execution"] = None
    else:
        execution = dict(scenario.execution)
        if seed is not None:
            execution["seed"] = seed
        raw["execution"] = execution
    return AppConfig.model_validate(raw)


def _metrics(result: BacktestResult, config: AppConfig) -> dict[str, float]:
    m = compute_metrics(
        result.equity,
        result.initial_capital,
        result.trades,
        result.exposure,
        config.periods_per_year,
        result.fills,
    )
    out = {k: float(m.get(k, 0.0)) for k in REPORTED}
    events = result.execution_events
    out["rejected_orders"] = float(sum(1 for e in events if e.status == "rejected"))
    out["partial_fills"] = float(sum(1 for e in events if e.status == "partial"))
    out["delisting_liquidations"] = float(
        sum(1 for t in result.trades if t.exit_reason is OrderReason.DELISTING)
    )
    return out


def _passive(equity: pd.Series, capital: float, config: AppConfig) -> dict[str, float]:
    m = compute_metrics(equity, capital, periods=config.periods_per_year)
    out = {k: float(m.get(k, 0.0)) for k in REPORTED}
    out["average_exposure"] = 1.0 if equity.iloc[-1] != capital else 0.0
    out["time_in_market"] = out["average_exposure"]
    return out


def _run(
    config: AppConfig,
    scenario: Scenario,
    data: dict[str, pd.DataFrame],
    signals: dict[str, pd.DataFrame],
    hourly: dict[str, pd.DataFrame] | None,
    start: str,
    end: str,
) -> tuple[dict[str, float], BacktestResult, list[float], AppConfig]:
    """Run a scenario (over all its seeds); return the median run's metrics."""
    runs: list[tuple[dict[str, float], BacktestResult, AppConfig]] = []
    for draw in range(scenario.seeds):
        cfg = scenario_config(config, scenario, seed=draw if scenario.seeds > 1 else None)
        result = run_backtest(
            cfg,
            data,
            start=start,
            end=end,
            signals=signals,
            intraday=hourly if scenario.needs_hourly else None,
        )
        runs.append((_metrics(result, cfg), result, cfg))
    runs.sort(key=lambda r: r[0]["final_equity"])
    metrics, result, cfg = runs[len(runs) // 2]
    return metrics, result, [r[0]["cagr"] for r in runs], cfg


def yearly(result: BacktestResult) -> dict[int, dict[str, float]]:
    """Return and costs of each calendar year."""
    equity = result.equity
    years = pd.DatetimeIndex(equity.index).year
    previous = equity.shift(1)
    previous.iloc[0] = result.initial_capital
    returns = equity / previous - 1.0
    out: dict[int, dict[str, float]] = {}
    for year in sorted(set(years)):
        mask = years == year
        fills = [f for f in result.fills if pd.Timestamp(f.timestamp).year == year]
        start_equity = float(previous[mask].iloc[0])
        costs = sum(f.fee + f.spread_cost + f.impact_cost for f in fills)
        out[int(year)] = {
            "return": float(np.prod(1.0 + returns[mask].to_numpy(float)) - 1.0),
            "max_drawdown": max(0.0, max_drawdown(equity[mask])),
            "costs": float(costs),
            "costs_pct_of_start_equity": float(costs / start_equity) if start_equity else 0.0,
            "fills": float(len(fills)),
        }
    return out


def _passive_yearly(equity: pd.Series, capital: float) -> dict[int, float]:
    previous = equity.shift(1)
    previous.iloc[0] = capital
    returns = equity / previous - 1.0
    years = pd.DatetimeIndex(equity.index).year
    return {
        int(y): float(np.prod(1.0 + returns[years == y].to_numpy(float)) - 1.0)
        for y in sorted(set(years))
    }


def run_study(
    config: AppConfig,
    data: dict[str, pd.DataFrame],
    hourly: dict[str, pd.DataFrame],
    start: str,
    end: str,
    ledger: ExperimentLedger | None = None,
    dataset_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run every scenario, the per-asset and per-year breakdowns, the break-even
    sweep and the benchmarks. ``data`` and ``hourly`` must already be restricted
    to the development segment."""
    capital = config.portfolio.initial_capital
    strategy = build_strategy(config.strategy)
    signals = compute_signal_frames(strategy, data)
    common = {
        "study": STUDY,
        "dataset": dataset_ids or {},
        "segment": "development",
        "period": [start, end],
        "symbols": sorted(data),
        "timeframe": config.market.timeframe,
        "strategy": f"{strategy.name} v{strategy.version}",
        "data_version": data_version(data)["id"],
    }

    def record(
        name: str, cfg: AppConfig, result: BacktestResult, metrics: dict[str, float], **extra: Any
    ) -> None:
        if ledger is None:
            return
        ledger.append(
            "experiment",
            **common,
            scenario=name,
            config_fingerprint=cfg.fingerprint(),
            result_hash=result_hash(result.equity),
            metrics={k: round(v, 6) for k, v in metrics.items()},
            **extra,
        )

    # ------------------------------------------------------------ benchmarks
    legacy_costs = config.costs
    bh = buy_and_hold(
        data, capital, legacy_costs, pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    )
    btc = next(s for s in data if s.startswith("BTC"))
    bh_btc = buy_and_hold(
        {btc: data[btc]},
        capital,
        legacy_costs,
        pd.Timestamp(start, tz="UTC"),
        pd.Timestamp(end, tz="UTC"),
    )
    cash = pd.Series(capital, index=bh.index, name="cash")

    # ------------------------------------------------------------- scenarios
    scenarios: list[dict[str, Any]] = []
    results: dict[str, BacktestResult] = {}
    for scenario in SCENARIOS:
        metrics, result, cagrs, cfg = _run(config, scenario, data, signals, hourly, start, end)
        results[scenario.name] = result
        row: dict[str, Any] = {
            "name": scenario.name,
            "group": scenario.group,
            "description": scenario.description,
            "basis": scenario.basis,
            "metrics": metrics,
            "significance": {
                **sharpe_statistics(result.returns, config.periods_per_year),
                **trade_significance([t.return_pct for t in result.trades], config.validation.seed),
            },
        }
        if scenario.seeds > 1:
            row["draws"] = {
                "seeds": scenario.seeds,
                "cagr_min": min(cagrs),
                "cagr_median": float(np.median(cagrs)),
                "cagr_max": max(cagrs),
            }
        scenarios.append(row)
        record(scenario.name, cfg, result, metrics, draws=row.get("draws"))

    # matched benchmark: buy & hold scaled to the base scenario's average exposure
    weight = next(r for r in scenarios if r["name"] == "base")["metrics"]["average_exposure"]
    matched = (capital * (1.0 - weight) + bh * weight).rename("matched")
    benchmarks = {
        "cash": _passive(cash, capital, config),
        "buy_and_hold_equal_weight": _passive(bh, capital, config),
        "buy_and_hold_btc": _passive(bh_btc, capital, config),
        "exposure_matched_buy_and_hold": {
            **_passive(matched, capital, config),
            "average_exposure": weight,
        },
    }
    for name, curve in (
        ("buy_and_hold_equal_weight", bh),
        ("exposure_matched_buy_and_hold", matched),
    ):
        benchmarks[name]["sharpe_statistics"] = sharpe_statistics(
            (curve / curve.shift(1).fillna(capital) - 1.0), config.periods_per_year
        )

    # -------------------------------------------------------------- per asset
    by_name = {s.name: s for s in SCENARIOS}
    per_asset: dict[str, dict[str, Any]] = {}
    for symbol in sorted(data):
        alone = {symbol: data[symbol]}
        hour_alone = {symbol: hourly[symbol]} if symbol in hourly else {}
        rows: dict[str, Any] = {}
        for name in KEY_SCENARIOS:
            metrics, result, _, cfg = _run(
                config, by_name[name], alone, {symbol: signals[symbol]}, hour_alone, start, end
            )
            rows[name] = metrics
            record(f"{name}@{symbol}", cfg, result, metrics)
        bench = buy_and_hold(
            alone, capital, legacy_costs, pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
        )
        rows["buy_and_hold"] = _passive(bench, capital, config)
        per_asset[symbol] = rows

    # -------------------------------------------------------------- per year
    per_year = {name: yearly(results[name]) for name in KEY_SCENARIOS}
    per_year["buy_and_hold_equal_weight"] = {
        y: {"return": r} for y, r in _passive_yearly(bh, capital).items()
    }

    # ------------------------------------------------------ break-even sweep
    sweep = []
    for bps in SWEEP_BPS:
        swept = Scenario(
            f"uniform_{bps:g}bps",
            "sweep",
            f"{bps:g} bps per fill, fee included",
            "assumed",
            costs={"fee_rate": 0.0, "slippage_bps": float(bps)},
        )
        metrics, result, _, cfg = _run(config, swept, data, signals, None, start, end)
        sweep.append({"bps_per_fill": float(bps), **metrics})
        record(swept.name, cfg, result, metrics)

    return {
        "study": STUDY,
        "period": [start, end],
        "symbols": sorted(data),
        "capital": capital,
        "config_fingerprint": config.fingerprint(),
        "data_version": common["data_version"],
        "dataset": dataset_ids or {},
        "scenarios": scenarios,
        "benchmarks": benchmarks,
        "per_asset": per_asset,
        "per_year": per_year,
        "sweep": sweep,
        "break_even": break_even(sweep, benchmarks),
    }


def break_even(sweep: list[dict[str, Any]], benchmarks: dict[str, Any]) -> dict[str, float]:
    """Cost per fill (bps) at which the strategy stops beating each reference,
    interpolated linearly between swept points; NaN if never reached."""

    def crossing(metric: str, threshold: float) -> float:
        for a, b in itertools.pairwise(sweep):
            if a[metric] > threshold >= b[metric]:
                span = a[metric] - b[metric]
                share = (a[metric] - threshold) / span if span else 0.0
                return float(a["bps_per_fill"] + share * (b["bps_per_fill"] - a["bps_per_fill"]))
        return 0.0 if sweep and sweep[0][metric] <= threshold else math.nan

    return {
        "cagr_zero_bps": crossing("cagr", 0.0),
        "cagr_matched_benchmark_bps": crossing(
            "cagr", benchmarks["exposure_matched_buy_and_hold"]["cagr"]
        ),
        "sharpe_buy_and_hold_bps": crossing(
            "sharpe", benchmarks["buy_and_hold_equal_weight"]["sharpe"]
        ),
        "sharpe_matched_benchmark_bps": crossing(
            "sharpe", benchmarks["exposure_matched_buy_and_hold"]["sharpe"]
        ),
    }


def timeframe_diagnostic(
    config: AppConfig,
    data: dict[str, pd.DataFrame],
    timeframe: str,
    start: str,
    end: str,
    ledger: ExperimentLedger | None = None,
    dataset_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The same rules, with the same parameters *in candles*, on ``timeframe``
    candles: how fast do costs eat a strategy when it trades more often?

    This is **not** a validation of any intraday strategy: the parameters were
    never chosen for this timeframe (55 four-hour candles are nine days, not 55),
    and nothing measured at 1h or 4h says anything about 5m or 15m.
    """
    raw = config.model_dump()
    raw["market"]["timeframe"] = timeframe
    cfg = AppConfig.model_validate(raw)
    strategy = build_strategy(cfg.strategy)
    signals = compute_signal_frames(strategy, data)
    out: dict[str, Any] = {"timeframe": timeframe, "scenarios": {}}
    by_name = {s.name: s for s in SCENARIOS}
    for name in ("no_costs", "legacy", "base", "high"):
        metrics, result, _, run_cfg = _run(cfg, by_name[name], data, signals, None, start, end)
        out["scenarios"][name] = metrics
        if ledger is not None:
            ledger.append(
                "experiment",
                study=STUDY,
                dataset=dataset_ids or {},
                segment="development",
                period=[start, end],
                symbols=sorted(data),
                timeframe=timeframe,
                strategy=f"{strategy.name} v{strategy.version}",
                data_version=data_version(data)["id"],
                scenario=f"{name}@{timeframe}-diagnostic",
                config_fingerprint=run_cfg.fingerprint(),
                result_hash=result_hash(result.equity),
                metrics={k: round(v, 6) for k, v in metrics.items()},
                note="diagnostic only: parameters not chosen for this timeframe",
            )
    bench = buy_and_hold(
        data,
        cfg.portfolio.initial_capital,
        cfg.costs,
        pd.Timestamp(start, tz="UTC"),
        pd.Timestamp(end, tz="UTC"),
    )
    out["buy_and_hold"] = _passive(bench, cfg.portfolio.initial_capital, cfg)
    return out


# ------------------------------------------------------------------- rendering
def _pct(value: float) -> str:
    return "n/a" if not math.isfinite(value) else f"{value * 100:.1f}%"


def _num(value: float, digits: int = 2) -> str:
    return "n/a" if not math.isfinite(value) else f"{value:.{digits}f}"


_COLUMNS: tuple[tuple[str, str, Any], ...] = (
    ("CAGR", "cagr", _pct),
    ("Sharpe", "sharpe", _num),
    ("Sortino", "sortino", _num),
    ("Calmar", "calmar", _num),
    ("Max DD", "max_drawdown", _pct),
    ("PF", "profit_factor", _num),
    ("Win", "win_rate", _pct),
    ("Expect.", "expectancy", _pct),
    ("Turnover/y", "turnover", lambda v: _num(v, 1)),
    ("Trades", "trades", lambda v: f"{v:.0f}"),
    ("Costs $", "total_costs", lambda v: f"{v:,.0f}"),
    ("Cost drag/y", "cost_drag", _pct),
    ("Avg exp.", "average_exposure", _pct),
)


def _table(rows: list[tuple[str, dict[str, float]]]) -> list[str]:
    head = "| Scenario | " + " | ".join(c[0] for c in _COLUMNS) + " |"
    lines = [head, "|" + "---|" * (len(_COLUMNS) + 1)]
    for name, m in rows:
        cells = " | ".join(fmt(float(m.get(key, math.nan))) for _, key, fmt in _COLUMNS)
        lines.append(f"| {name} | {cells} |")
    return lines


def render(report: dict[str, Any]) -> str:
    """Markdown tables of a study (numbers only; interpretation lives in the docs)."""
    out = [
        f"# Execution-cost study `{report['study']}`",
        "",
        f"Development segment only, {report['period'][0]} -> {report['period'][1]}, "
        f"{', '.join(report['symbols'])}, capital {report['capital']:,.0f}. "
        f"Config fingerprint `{report['config_fingerprint']}`, data version "
        f"`{report['data_version']}`.",
        "",
        "Expectancy is the mean return per closed trade. Costs are net of nothing: "
        "fees + spread + impact actually charged.",
        "",
        "## Scenarios",
        "",
    ]
    out += _table([(r["name"], r["metrics"]) for r in report["scenarios"]])
    out += [
        "",
        "| Scenario | Basis | Rejected | Partial | Sharpe 95% CI | P(Sharpe>0) | "
        "Trade p-value | Draws (CAGR min/median/max) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in report["scenarios"]:
        s, m = r["significance"], r["metrics"]
        draws = r.get("draws")
        spread = (
            f"{_pct(draws['cagr_min'])} / {_pct(draws['cagr_median'])} / {_pct(draws['cagr_max'])}"
            if draws
            else "-"
        )
        out.append(
            f"| {r['name']} | {r['basis']} | {m['rejected_orders']:.0f} | "
            f"{m['partial_fills']:.0f} | [{_num(s['ci_low'])}, {_num(s['ci_high'])}] | "
            f"{_pct(s['prob_positive'])} | {_num(s['p_value'], 3)} | {spread} |"
        )
    out += ["", "## Benchmarks", ""]
    out += _table(list(report["benchmarks"].items()))
    out += ["", "## Per asset", ""]
    for symbol, rows in report["per_asset"].items():
        out += [f"### {symbol}", "", *_table(list(rows.items())), ""]
    out += ["## Per year (return; costs as % of equity at the start of the year)", ""]
    years = sorted({y for rows in report["per_year"].values() for y in rows})
    out.append("| Scenario | " + " | ".join(str(y) for y in years) + " |")
    out.append("|" + "---|" * (len(years) + 1))
    for name, rows in report["per_year"].items():
        cells = []
        for y in years:
            row = rows.get(y) or rows.get(str(y)) or {}
            text = _pct(row.get("return", math.nan))
            if "costs_pct_of_start_equity" in row:
                text += f" ({row['costs_pct_of_start_equity'] * 100:.2f}%)"
            cells.append(text)
        out.append(f"| {name} | " + " | ".join(cells) + " |")
    out += ["", "## Break-even: uniform cost per fill (fee included)", ""]
    out += _table([(f"{r['bps_per_fill']:g} bps", r) for r in report["sweep"]])
    out += ["", "| Break-even point | bps per fill |", "|---|---|"]
    for key, value in report["break_even"].items():
        out.append(f"| {key} | {_num(value, 1)} |")
    for diag in report.get("timeframe_diagnostics", []):
        out += [
            "",
            f"## Diagnostic only: same rules on {diag['timeframe']} candles",
            "",
            "Parameters were never chosen for this timeframe. This is not a "
            "validation and says nothing about 5m/15m.",
            "",
        ]
        out += _table([*diag["scenarios"].items(), ("buy_and_hold", diag["buy_and_hold"])])
    return "\n".join(out) + "\n"
