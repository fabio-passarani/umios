"""Command line interface: ``qts <group> <command>``.

All commands share one error policy (:func:`_guard`): expected failures
(:class:`QtsError`) are logged once, recorded as a system event when the
database is reachable, and turned into exit code 1 — no tracebacks for
operational problems, full tracebacks for real bugs.
"""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
import yaml

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import run_backtest
from qts.backtesting.metrics import compute_metrics
from qts.backtesting.validation import run_validation
from qts.core.config import AppConfig, Settings, enforce_mode, load_config
from qts.core.errors import ConfigError, QtsError
from qts.core.logging import configure_logging, get_logger
from qts.core.types import OrderReason, TradingMode
from qts.execution.paper import PaperTrader
from qts.integrations.db.storage import Storage
from qts.market_data.freqtrade_export import export_to_freqtrade
from qts.market_data.schema import check_quality
from qts.market_data.service import load_universe, update_history
from qts.market_data.sources import BinanceVisionSource
from qts.market_data.store import ParquetStore
from qts.monitoring.health import run_health_checks
from qts.reporting.persist import save_backtest
from qts.reporting.report import HEADLINE_METRICS, format_metric, write_validation_report
from qts.risk.kill_switch import KillSwitch

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Quant Trading System")
data_app = typer.Typer(no_args_is_help=True, help="Historical market data")
paper_app = typer.Typer(no_args_is_help=True, help="Paper trading (simulated, no real money)")
risk_app = typer.Typer(no_args_is_help=True, help="Risk engine and kill switch")
db_app = typer.Typer(no_args_is_help=True, help="Operational database")
app.add_typer(data_app, name="data")
app.add_typer(paper_app, name="paper")
app.add_typer(risk_app, name="risk")
app.add_typer(db_app, name="db")

log = get_logger("cli")
F = TypeVar("F", bound=Callable[..., Any])
ConfigOption = Annotated[
    Path | None, typer.Option("--config", "-c", help="YAML config (default: $QTS_CONFIG_FILE)")
]


class Runtime:
    """Everything a command needs, built once from settings + config."""

    def __init__(self, config_path: Path | None) -> None:
        self.settings = Settings()
        configure_logging(self.settings.log_level, self.settings.log_json)
        self.config: AppConfig = load_config(config_path or self.settings.config_file)
        self.mode: TradingMode = enforce_mode(self.config, self.settings)
        self.store = ParquetStore(self.config.data.directory)
        self.kill_switch = KillSwitch(self.settings.state_dir / "kill_switch.json")

    @functools.cached_property
    def storage(self) -> Storage:
        storage = Storage(self.settings.database_url.get_secret_value())
        storage.init_schema()
        return storage


def _guard(func: F) -> F:
    """Centralised error handling for CLI commands."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except QtsError as exc:
            log.error(
                "command_failed", command=func.__name__, error=type(exc).__name__, detail=str(exc)
            )
            typer.secho(f"ERROR: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

    return wrapper  # type: ignore[return-value]


def _print_metrics(columns: dict[str, dict[str, float]]) -> None:
    names = list(columns)
    typer.echo(f"{'':24}" + "".join(f"{n:>18}" for n in names))
    for key, label, kind in HEADLINE_METRICS:
        if any(key in columns[n] for n in names):
            cells = "".join(
                f"{format_metric(columns[n].get(key), kind) if key in columns[n] else '-':>18}"
                for n in names
            )
            typer.echo(f"{label:24}{cells}")


# --------------------------------------------------------------------------- root
@app.command("config")
@_guard
def show_config(config: ConfigOption = None) -> None:
    """Validate the configuration and print it with its fingerprint."""
    rt = Runtime(config)
    typer.echo(f"# mode: {rt.mode.value}   fingerprint: {rt.config.fingerprint()}")
    typer.echo(yaml.safe_dump(rt.config.model_dump(mode="json"), sort_keys=False))


@app.command()
@_guard
def backtest(
    config: ConfigOption = None,
    start: Annotated[str | None, typer.Option(help="First trading day (YYYY-MM-DD)")] = None,
    end: Annotated[str | None, typer.Option(help="Last trading day (YYYY-MM-DD)")] = None,
    save: Annotated[bool, typer.Option(help="Store the run in the database")] = True,
) -> None:
    """Backtest the configured strategy and compare it with Buy & Hold."""
    rt = Runtime(config)
    data = load_universe(rt.config, rt.store)
    result = run_backtest(rt.config, data, start=start, end=end)
    capital = rt.config.portfolio.initial_capital
    bench = buy_and_hold(
        data, capital, rt.config.costs, result.equity.index[0], result.equity.index[-1]
    )
    metrics = compute_metrics(result.equity, capital, result.trades, result.exposure)
    typer.echo(
        f"Backtest {result.metadata['start'][:10]} -> {result.metadata['end'][:10]}  "
        f"config {result.config_fingerprint}"
    )
    _print_metrics({"Strategy": metrics, "Buy & Hold": compute_metrics(bench, capital)})
    rejected = sum(1 for d in result.decisions if not d.approved)
    typer.echo(
        f"Risk engine: {len(result.decisions)} orders evaluated, {rejected} rejected; "
        f"kill switch {'TRIPPED' if result.kill_switch_active else 'not tripped'}"
    )
    if save:
        run_id = save_backtest(rt.storage, result, metrics, bench)
        typer.echo(f"Saved as run {run_id}")


@app.command()
@_guard
def validate(
    config: ConfigOption = None,
    output: Annotated[Path, typer.Option(help="Reports directory")] = Path("reports"),
    tearsheet: Annotated[bool, typer.Option(help="Also write a QuantStats tearsheet")] = True,
) -> None:
    """Run the full validation pipeline (IS/OOS, walk-forward, Monte Carlo)."""
    rt = Runtime(config)
    report = run_validation(rt.config, load_universe(rt.config, rt.store))
    directory = write_validation_report(report, output, tearsheet=tearsheet)
    for sample in (report.full, report.in_sample, report.out_of_sample):
        typer.echo(f"\n== {sample.label} {sample.start:%Y-%m-%d} -> {sample.end:%Y-%m-%d} ==")
        _print_metrics({"Strategy": sample.strategy, "Buy & Hold": sample.benchmark})
    wf = report.walk_forward
    typer.echo(f"\n== walk-forward ({len(wf.windows)} windows, efficiency {wf.efficiency:.2f}) ==")
    _print_metrics({"WF out-of-sample": wf.oos_metrics, "Buy & Hold": wf.benchmark_metrics})
    typer.echo(
        f"\nMonte Carlo: P(loss) = {report.monte_carlo.probability_of_loss:.1%}, "
        f"median max drawdown = {report.monte_carlo.max_drawdown['p50']:.1%}"
    )
    typer.echo("\nWarnings:")
    for warning in report.warnings:
        typer.echo(f"  - {warning}")
    typer.echo(f"\nReport written to {directory}")


@app.command()
@_guard
def health(config: ConfigOption = None) -> None:
    """Check database, data freshness, kill switch and service heartbeats."""
    rt = Runtime(config)
    checks = run_health_checks(rt.config, rt.storage, rt.store, rt.kill_switch)
    for check in checks:
        typer.echo(f"[{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- data
@data_app.command("download")
@_guard
def data_download(config: ConfigOption = None) -> None:
    """Download or refresh history for every configured symbol."""
    rt = Runtime(config)
    reports = update_history(rt.config, BinanceVisionSource(), rt.store)
    for symbol, report in reports.items():
        typer.echo(
            f"{symbol}: {report.rows} candles {report.start:%Y-%m-%d} -> {report.end:%Y-%m-%d}, "
            f"{report.missing_candles} missing"
        )


@data_app.command("check")
@_guard
def data_check(config: ConfigOption = None) -> None:
    """Run quality checks on stored data."""
    rt = Runtime(config)
    failed = False
    for symbol, frame in load_universe(rt.config, rt.store).items():
        report = check_quality(frame, rt.config.market.timeframe)
        failed |= not report.ok
        typer.echo(
            f"{symbol}: {'OK' if report.ok else 'FAIL'} rows={report.rows} "
            f"errors={report.errors} warnings={report.warnings}"
        )
    if failed:
        raise typer.Exit(code=1)


@data_app.command("export-freqtrade")
@_guard
def data_export_freqtrade(
    config: ConfigOption = None,
    datadir: Annotated[Path, typer.Option(help="Freqtrade data directory")] = Path(
        "freqtrade/user_data/data/binance"
    ),
) -> None:
    """Export stored candles in Freqtrade's feather format."""
    rt = Runtime(config)
    for path in export_to_freqtrade(rt.config, rt.store, datadir):
        typer.echo(f"wrote {path}")


# -------------------------------------------------------------------------- paper
def _trader(rt: Runtime, refresh: bool) -> PaperTrader:
    if rt.mode is not TradingMode.PAPER:
        raise ConfigError("this command needs a configuration with mode: paper")
    source = BinanceVisionSource() if refresh else None
    return PaperTrader(rt.config, rt.storage, rt.store, rt.kill_switch, source)


@paper_app.command("start")
@_guard
def paper_start(
    config: ConfigOption = None,
    start_from: Annotated[
        str | None, typer.Option("--from", help="Replay history from this date (demo only)")
    ] = None,
) -> None:
    """Open a new paper-trading session."""
    rt = Runtime(config)
    typer.echo(f"Paper session started: {_trader(rt, False).start(start_from)}")


@paper_app.command("step")
@_guard
def paper_step(
    config: ConfigOption = None,
    refresh: Annotated[bool, typer.Option(help="Download new candles first")] = True,
) -> None:
    """Process candles closed since the last step (safe to run repeatedly)."""
    rt = Runtime(config)
    report = _trader(rt, refresh).step()
    typer.echo(json.dumps({k: str(v) for k, v in report.__dict__.items()}, indent=2))


@paper_app.command("run")
@_guard
def paper_run(
    config: ConfigOption = None,
    interval_minutes: Annotated[int, typer.Option(min=1)] = 60,
) -> None:
    """Long-running service: start a session if needed, then step periodically."""
    rt = Runtime(config)
    trader = _trader(rt, True)
    if rt.storage.active_paper_run() is None:
        try:
            trader.start()
        except QtsError:
            update_history(rt.config, BinanceVisionSource(), rt.store)
            trader.start()
    while True:
        try:
            trader.step()
        except QtsError as exc:  # keep the service alive; the error is already recorded
            log.error("paper_step_failed", error=type(exc).__name__, detail=str(exc))
        time.sleep(interval_minutes * 60)


@paper_app.command("stop")
@_guard
def paper_stop(config: ConfigOption = None) -> None:
    """Close the active paper session."""
    rt = Runtime(config)
    typer.echo(f"Stopped: {_trader(rt, False).stop()}")


@paper_app.command("close")
@_guard
def paper_close(
    config: ConfigOption = None,
    symbol: Annotated[
        list[str] | None, typer.Option(help="Symbol to close (repeatable); omit with --all")
    ] = None,
    everything: Annotated[bool, typer.Option("--all", help="Close every open position")] = False,
    fraction: Annotated[
        float, typer.Option(min=0.0001, max=1.0, help="Share of each position to close")
    ] = 1.0,
    emergency: Annotated[
        bool, typer.Option(help="Record the exit as an emergency instead of manual")
    ] = False,
) -> None:
    """Close or reduce open paper positions at the next open (works with the kill switch on)."""
    rt = Runtime(config)
    if bool(symbol) == everything:
        raise ConfigError("give either --symbol (one or more) or --all")
    reason = OrderReason.EMERGENCY if emergency else OrderReason.MANUAL
    queued = _trader(rt, False).request_exit(None if everything else symbol, reason, fraction)
    typer.echo(f"Exit queued for: {', '.join(queued) if queued else 'nothing (no open position)'}")


@paper_app.command("status")
@_guard
def paper_status(config: ConfigOption = None) -> None:
    """Show the active paper session: equity, positions, recent risk rejections."""
    rt = Runtime(config)
    run = rt.storage.active_paper_run()
    if run is None:
        typer.echo("No active paper session.")
        return
    typer.echo(
        f"Run {run.id}  started {run.started_at:%Y-%m-%d %H:%M}  config {run.config_fingerprint}"
    )
    curve = rt.storage.equity_curve(run.id)
    if not curve.empty:
        last = curve.iloc[-1]
        typer.echo(
            f"Equity {last['equity']:,.2f}  cash {last['cash']:,.2f}  "
            f"drawdown {last['drawdown']:.2%}  as of {curve.index[-1]:%Y-%m-%d}"
        )
    for p in rt.storage.positions(run.id):
        typer.echo(
            f"  {p.symbol}: {p.quantity:.6f} @ {p.avg_price:,.2f}  stop {p.stop_price}  "
            f"uPnL {p.unrealized_pnl:,.2f}"
        )
    rejected = [d for d in rt.storage.risk_decisions(run.id) if not d.approved][-5:]
    for d in rejected:
        typer.echo(f"  rejected {d.ts:%Y-%m-%d} {d.symbol}: {d.failed_checks}")


# --------------------------------------------------------------------------- risk
@risk_app.command("status")
@_guard
def risk_status(config: ConfigOption = None) -> None:
    """Show kill-switch state and configured limits."""
    rt = Runtime(config)
    state = rt.kill_switch.state
    typer.echo(f"Kill switch: {'ACTIVE' if state.active else 'not active'} {state.reason}")
    typer.echo(yaml.safe_dump(rt.config.risk.model_dump(mode="json"), sort_keys=False))


@risk_app.command("kill")
@_guard
def risk_kill(
    reason: Annotated[str, typer.Option(help="Why trading is being halted")],
    config: ConfigOption = None,
) -> None:
    """Trip the kill switch: no new entries until it is reset by a human."""
    rt = Runtime(config)
    rt.kill_switch.activate(reason, source="operator")
    rt.storage.record_event("error", "risk", f"kill switch activated by operator: {reason}")
    typer.echo("Kill switch ACTIVE.")


@risk_app.command("reset")
@_guard
def risk_reset(
    operator: Annotated[str, typer.Option(help="Who takes responsibility for resuming")],
    config: ConfigOption = None,
) -> None:
    """Reset the kill switch and re-base the drawdown measure of the paper session."""
    rt = Runtime(config)
    rt.kill_switch.reset(operator)
    run = rt.storage.active_paper_run()
    if run is not None:
        state = rt.storage.load_paper_state(run.id)
        if state is not None:
            state["risk"]["peak_equity"] = state["risk"].get("last_equity")
            rt.storage.save_paper_state(run.id, state)
    rt.storage.record_event("warning", "risk", f"kill switch reset by {operator}")
    typer.echo("Kill switch reset.")


# ----------------------------------------------------------------------------- db
@db_app.command("init")
@_guard
def db_init(config: ConfigOption = None) -> None:
    """Create the database schema (idempotent)."""
    rt = Runtime(config)
    _ = rt.storage
    typer.echo("Database schema ready.")


if __name__ == "__main__":  # pragma: no cover
    app()
