"""Reproducible result bundles.

Every report directory contains the numbers *and* the exact configuration that
produced them (plus its fingerprint), so a result can always be traced back and
re-run. Nothing here computes performance: it only serialises what the
backtesting package measured.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from qts import __version__
from qts.backtesting.engine import BacktestResult
from qts.backtesting.validation import SampleReport, ValidationReport
from qts.core.logging import get_logger

log = get_logger("reporting")

HEADLINE_METRICS = (
    ("total_return", "Total return", "pct"),
    ("cagr", "CAGR", "pct"),
    ("annual_volatility", "Annual volatility", "pct"),
    ("sharpe", "Sharpe ratio", "num"),
    ("sortino", "Sortino ratio", "num"),
    ("max_drawdown", "Max drawdown", "pct"),
    ("calmar", "Calmar ratio", "num"),
    ("trades", "Closed trades", "int"),
    ("win_rate", "Win rate", "pct"),
    ("profit_factor", "Profit factor", "num"),
    ("expectancy", "Expectancy per trade", "money"),
    ("time_in_market", "Time in market", "pct"),
    ("total_fees", "Fees paid", "money"),
)


def format_metric(value: float | None, kind: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    if kind == "pct":
        return f"{value:.2%}"
    if kind == "int":
        return f"{int(value)}"
    if kind == "money":
        return f"{value:,.2f}"
    return f"{value:.2f}"


def _clean(value: Any) -> Any:
    """Make nested structures JSON-serialisable (timestamps, NaN, infinity)."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, pd.Timestamp | datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None if math.isnan(value) else ("inf" if value > 0 else "-inf")
    return value


def sample_to_dict(sample: SampleReport) -> dict[str, Any]:
    return {
        "label": sample.label,
        "start": sample.start,
        "end": sample.end,
        "strategy": sample.strategy,
        "buy_and_hold": sample.benchmark,
    }


def validation_to_dict(report: ValidationReport) -> dict[str, Any]:
    """Full validation study as a plain dictionary."""
    wf = report.walk_forward
    payload: dict[str, Any] = _clean(
        {
            "generated_at": datetime.now(tz=UTC),
            "qts_version": __version__,
            "config_fingerprint": report.config_fingerprint,
            "config": report.config,
            "periods": {
                s.label: sample_to_dict(s)
                for s in (report.full, report.in_sample, report.out_of_sample)
            },
            "walk_forward": {
                "parameter_grid": wf.grid,
                "efficiency": wf.efficiency,
                "out_of_sample": wf.oos_metrics,
                "buy_and_hold": wf.benchmark_metrics,
                "windows": [asdict(w) for w in wf.windows],
            },
            "monte_carlo": asdict(report.monte_carlo),
            "out_of_sample_start": report.split,
            "provenance": report.provenance,
            "robustness": asdict(report.robustness) if report.robustness else None,
            "warnings": report.warnings,
        }
    )
    return payload


def _metrics_table(columns: dict[str, dict[str, float]]) -> list[str]:
    names = list(columns)
    lines = ["| Metric | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    for key, label, kind in HEADLINE_METRICS:
        if not any(key in columns[n] for n in names):
            continue
        cells = [
            format_metric(columns[n].get(key), kind) if key in columns[n] else "—" for n in names
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return lines


def render_markdown(report: ValidationReport) -> str:
    """Human-readable validation report."""
    wf, mc = report.walk_forward, report.monte_carlo
    out: list[str] = [
        "# Validation report",
        "",
        f"- Config fingerprint: `{report.config_fingerprint}`",
        f"- Strategy: `{report.config['strategy']['name']}` on "
        f"{', '.join(report.config['market']['symbols'])} ({report.config['market']['timeframe']})",
        f"- Costs: fee {report.config['costs']['fee_rate']:.4%} + slippage "
        f"{report.config['costs']['slippage_bps']} bps per fill",
        f"- Generated: {datetime.now(tz=UTC):%Y-%m-%d %H:%M UTC} — qts {__version__}",
        "",
        "> Past performance of a simulation is not evidence of future returns. Read the "
        "warnings at the end before drawing any conclusion.",
        "",
    ]
    out += _provenance_markdown(report)
    for sample in (report.full, report.in_sample, report.out_of_sample):
        out += [
            f"## {sample.label.replace('_', ' ').title()} "
            f"({sample.start:%Y-%m-%d} → {sample.end:%Y-%m-%d})",
            "",
            *_metrics_table({"Strategy": sample.strategy, "Buy & Hold": sample.benchmark}),
            "",
        ]
    out += [
        "## Walk-forward analysis",
        "",
        f"Stitched out-of-sample track over {len(wf.windows)} windows; parameters re-chosen on "
        f"each train window among `{json.dumps(_clean(wf.grid))}`. "
        f"Walk-forward efficiency (OOS Sharpe / mean train Sharpe): **{wf.efficiency:.2f}**.",
        "",
        *_metrics_table({"Walk-forward OOS": wf.oos_metrics, "Buy & Hold": wf.benchmark_metrics}),
        "",
        "| Test window | Chosen parameters | Train Sharpe | Test Sharpe | Test return | Trades |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for w in wf.windows:
        params = ", ".join(f"{k}={v:g}" for k, v in w.chosen_params.items())
        out.append(
            f"| {w.test_start:%Y-%m-%d} → {w.test_end:%Y-%m-%d} | {params} | "
            f"{w.train_sharpe:.2f} | {w.test_sharpe:.2f} | {w.test_return:.2%} | {w.test_trades} |"
        )
    out += [
        "",
        "## Monte Carlo (block bootstrap of daily returns)",
        "",
        f"{mc.simulations} simulations, block size {mc.block_size}, seed {mc.seed}. "
        f"Probability of ending with a loss: **{mc.probability_of_loss:.1%}**.",
        "",
        "| Statistic | Observed | P5 | P25 | Median | P75 | P95 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label, kind in (
        ("total_return", "Total return", "pct"),
        ("max_drawdown", "Max drawdown", "pct"),
        ("sharpe", "Sharpe ratio", "num"),
    ):
        dist = getattr(mc, key)
        cells = [format_metric(dist[p], kind) for p in ("p05", "p25", "p50", "p75", "p95")]
        out.append(
            f"| {label} | {format_metric(mc.observed[key], kind)} | " + " | ".join(cells) + " |"
        )
    out += _robustness_markdown(report)
    out += ["", "## Warnings and limits", ""] + [f"- {w}" for w in report.warnings] + [""]
    return "\n".join(out)


def _pct(value: float | None) -> str:
    return format_metric(value, "pct")


def _num(value: float | None) -> str:
    return format_metric(value, "num")


def _table(header: list[str], rows: list[list[str]], right_from: int = 1) -> list[str]:
    align = ["---" if i < right_from else "---:" for i in range(len(header))]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(align) + "|"]
    return lines + ["| " + " | ".join(row) + " |" for row in rows]


def _provenance_markdown(report: ValidationReport) -> list[str]:
    p = report.provenance
    if not p:
        return []
    code, period = p["code"], p["period"]
    commit = code["git_commit"][:12] + (
        " **with uncommitted changes**" if code["git_dirty"] else ""
    )
    lines = [
        "## Provenance",
        "",
        f"- Period: {period['start'][:10]} → {period['end'][:10]} ({period['bars']} bars), "
        + ("pinned" if period["pinned"] else "**not pinned (data.end is open)**"),
        f"- Out-of-sample starts: {report.split:%Y-%m-%d}"
        + (
            " (fixed date)"
            if report.config["validation"].get("out_of_sample_start")
            else " (**moving fraction**)"
        )
        if report.split is not None
        else "- Out-of-sample start: n/a",
        f"- Strategy: `{p['strategy']['name']}` version {p['strategy']['version']}",
        f"- Data version: `{p['data_version']['id']}`",
    ]
    for symbol, info in p["data_version"]["symbols"].items():
        lines.append(f"  - {symbol}: {info['rows']} candles, sha256 `{info['sha256'][:16]}…`")
    lines += [
        f"- Code: commit `{commit}`, qts {code['qts']}, Python {code['python']}, "
        f"numpy {code['numpy']}, pandas {code['pandas']}",
        f"- Result hash (full-period equity curve): `{p['result_hash']}`",
        f"- Reproducible: {'yes' if p['reproducible'] else '**no** (see warnings)'}",
        "",
    ]
    return lines


def _robustness_markdown(report: ValidationReport) -> list[str]:
    study = report.robustness
    if study is None:
        return []
    pct, num = _pct, _num
    out: list[str] = ["", "## Cost sensitivity", ""]
    out.append(
        "The same strategy, unchanged, under different frictions (full period). The configured "
        "costs are marked."
    )
    out.append("")
    base = (report.config["costs"]["fee_rate"], report.config["costs"]["slippage_bps"])
    out += _table(
        ["Fee per fill", "Slippage", "Total return", "CAGR", "Sharpe", "Max drawdown", "Trades"],
        [
            [
                f"{r['fee_rate']:.3%}"
                + (" ◀" if (r["fee_rate"], r["slippage_bps"]) == base else ""),
                f"{r['slippage_bps']:.0f} bps",
                pct(r["total_return"]),
                pct(r["cagr"]),
                num(r["sharpe"]),
                pct(r["max_drawdown"]),
                f"{int(r['trades'])}",
            ]
            for r in study.cost_sensitivity
        ],
    )

    s = study.parameter_summary
    out += ["", "## Parameter sensitivity", ""]
    out.append(
        "One parameter at a time moved by ±20% (a diagnostic: nothing is selected from this "
        f"table). {int(s['variants'])} variants: {pct(s['profitable_share'])} profitable, Sharpe "
        f"from {num(s['sharpe_min'])} to {num(s['sharpe_max'])} (median {num(s['sharpe_median'])}; "
        f"default {num(s['default_sharpe'])}); {pct(s['better_than_default_share'])} of the "
        "variants beat the default."
    )
    out.append("")
    out += _table(
        ["Parameter", "Value", "Total return", "Sharpe", "Max drawdown", "Trades"],
        [
            [
                str(r["parameter"]),
                "—" if r["value"] is None else f"{r['value']:g}",
                pct(r["total_return"]),
                num(r["sharpe"]),
                pct(r["max_drawdown"]),
                f"{int(r['trades'])}",
            ]
            for r in study.parameter_sensitivity
        ],
        right_from=1,
    )

    out += ["", "## Market regimes", ""]
    out.append(
        "Bull / bear / sideways from the trailing 90-day return of the equal-weight universe "
        "(above +20%, below -20%, in between) — a rule fixed in advance. Returns are compounded "
        "over the days in each regime."
    )
    out.append("")
    out += _table(
        ["Regime", "Days", "Share of time", "Strategy", "Buy & Hold", "Time in market"],
        [
            [
                r["regime"],
                str(r["days"]),
                pct(r["share_of_time"]),
                pct(r["strategy_return"]),
                pct(r["benchmark_return"]),
                pct(r["time_in_market"]),
            ]
            for r in study.regimes
        ],
    )
    out += ["", "## Calendar years", ""]
    out += _table(
        ["Year", "Strategy", "Buy & Hold", "Strategy max drawdown", "Trades closed"],
        [
            [
                str(r["year"]),
                pct(r["strategy_return"]),
                pct(r["benchmark_return"]),
                pct(r["max_drawdown"]),
                str(r["trades"]),
            ]
            for r in study.yearly
        ],
    )

    e, c = study.exposure, study.correlation
    out += ["", "## Exposure and correlation", ""]
    out += [
        f"- Time in market: {pct(e['time_in_market'])}; average exposure "
        f"{pct(e['average_exposure'])} of equity "
        f"({pct(e['average_exposure_when_invested'])} when invested), maximum "
        f"{pct(e['max_exposure'])}.",
        f"- Average number of open positions: {num(e['average_positions'])}; all assets held at "
        f"once {pct(e['time_all_assets_held'])} of the time.",
        "- Share of time each asset was held: "
        + ", ".join(f"{sym} {pct(v)}" for sym, v in e["by_symbol"].items())
        + ".",
        f"- Median holding period {e['median_holding_days']:.0f} days; longest stretch with no "
        f"position {e['longest_flat_days']} days.",
    ]
    for pair in c["pairs"]:
        out.append(
            f"- Daily-return correlation {pair['pair']}: {num(pair['correlation'])} "
            f"(rolling {c['window']}-day: min {num(pair['rolling_min'])}, median "
            f"{num(pair['rolling_median'])}, max {num(pair['rolling_max'])})."
        )
    if c["pairs"]:
        out.append(f"- Effective number of independent bets: {num(c['effective_bets'])}.")

    out += ["", "## Statistical significance", ""]
    rows = []
    for label, key in (("Full period", "full"), ("Out of sample", "out_of_sample")):
        g = study.significance[key]
        rows.append(
            [
                label,
                f"{int(g['trades'])}",
                pct(g["mean_return"]),
                num(g["t_stat"]),
                f"{g['p_value']:.3f}",
                num(g["sharpe"]),
                f"[{g['ci_low']:.2f}, {g['ci_high']:.2f}]",
                pct(g["prob_positive"]),
            ]
        )
    out += _table(
        [
            "Sample",
            "Trades",
            "Mean trade return",
            "t-stat",
            "p (mean ≤ 0)",
            "Sharpe",
            "95% interval",
            "P(Sharpe > 0)",
        ],
        rows,
    )
    out.append("")
    out.append(
        "p-value: one-sided bootstrap of the mean trade return under the null of zero expectancy. "
        "Sharpe interval: standard error adjusted for skewness and kurtosis of daily returns. "
        f"{study.significance['declared_trials']} parameter combinations were declared; no "
        "correction for multiple testing is applied to the figures above."
    )
    return out


def trades_frame(result: BacktestResult) -> pd.DataFrame:
    return pd.DataFrame([asdict(t) for t in result.trades])


def write_tearsheet(
    returns: pd.Series, benchmark: pd.Series, path: Path, title: str, periods: int = 365
) -> bool:
    """Write a QuantStats HTML tearsheet. Returns False if the optional
    ``reporting`` extra is not installed (the rest of the report is unaffected)."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import quantstats as qs
    except ImportError:
        log.warning(
            "tearsheet_skipped", reason="quantstats not installed (pip install .[reporting])"
        )
        return False
    strategy = returns.copy()
    strategy.index = pd.DatetimeIndex(strategy.index).tz_localize(None)
    bench = benchmark.pct_change().fillna(0.0)
    bench.index = pd.DatetimeIndex(bench.index).tz_localize(None)
    qs.reports.html(
        strategy.rename("Strategy"),
        benchmark=bench.rename("Buy & Hold"),
        output=str(path),
        title=title,
        periods_per_year=periods,
        compounded=True,
    )
    return True


def write_validation_report(report: ValidationReport, root: Path, tearsheet: bool = True) -> Path:
    """Write the full bundle to ``root/<utc timestamp>_<fingerprint>/`` and return it."""
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = root / f"{stamp}_{report.config_fingerprint}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(
        json.dumps(validation_to_dict(report), indent=2), encoding="utf-8"
    )
    (directory / "report.md").write_text(render_markdown(report), encoding="utf-8")
    (directory / "config.yaml").write_text(
        yaml.safe_dump(report.config, sort_keys=False), encoding="utf-8"
    )
    full = report.full
    curve = pd.DataFrame(
        {
            "equity": full.result.equity,
            "cash": full.result.cash,
            "exposure": full.result.exposure,
            "buy_and_hold": full.benchmark_equity,
        }
    )
    curve.to_csv(directory / "equity.csv")
    trades_frame(full.result).to_csv(directory / "trades.csv", index=False)
    report.walk_forward.oos_returns.to_csv(directory / "walk_forward_oos_returns.csv")
    if tearsheet:
        try:
            write_tearsheet(
                full.result.returns,
                full.benchmark_equity,
                directory / "tearsheet.html",
                f"{report.config['strategy']['name']} [{report.config_fingerprint}]",
            )
        except Exception as exc:
            log.warning("tearsheet_failed", error=type(exc).__name__, detail=str(exc)[:200])
    return directory
