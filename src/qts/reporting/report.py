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
    out += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings] + [""]
    return "\n".join(out)


def trades_frame(result: BacktestResult) -> pd.DataFrame:
    return pd.DataFrame([asdict(t) for t in result.trades])


def write_tearsheet(returns: pd.Series, benchmark: pd.Series, path: Path, title: str) -> bool:
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
        periods_per_year=365,
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
