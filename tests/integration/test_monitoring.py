"""Signals journal, monitoring cycles, the daily report and the soak verdict."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from qts.cli import app
from qts.core.config import AppConfig
from qts.integrations.db.storage import Storage
from qts.market_data.store import ParquetStore
from qts.monitoring.daily import (
    SOAK_DAYS,
    availability,
    build_daily_report,
    render_daily_report,
    soak_status,
)
from qts.monitoring.monitor import Monitor
from qts.risk.kill_switch import KillSwitch
from tests.conftest import random_walk
from tests.integration.conftest import SYMBOL
from tests.integration.test_paper_trading import START, publish, trader


def session(
    storage: Storage, cfg: AppConfig, candles: pd.DataFrame, parquet: ParquetStore, tmp_path: Path
) -> str:
    publish(parquet, candles, START)
    run_id = trader(cfg, storage, parquet, tmp_path).start()
    trader(cfg, storage, parquet, tmp_path).step(now=publish(parquet, candles, len(candles)))
    return run_id


def test_every_candle_leaves_a_signal_row_and_entries_follow_signals(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    run_id = session(storage, paper_config, candles, parquet, tmp_path)
    signals = storage.signals(run_id)
    assert len(signals) == len(candles) - START  # one per candle, also when nothing happened
    assert {s.action for s in signals} >= {"flat", "hold", "entry_queued", "exit_queued"}
    queued = {s.ts.replace(tzinfo=None): s for s in signals if s.action == "entry_queued"}
    days = [ts.tz_localize(None).to_pydatetime() for ts in candles.index]
    buys = [f for f in storage.fills(run_id) if f.side == "buy"]
    assert buys
    for fill in buys:  # the strategy's signal on the previous close caused each entry
        previous = days[days.index(fill.ts.replace(tzinfo=None)) - 1]
        assert previous in queued
        assert queued[previous].entry
        assert not queued[previous].in_position


def test_monitor_cycle_records_checks_and_reports_changes_once(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    session(storage, paper_config, candles, parquet, tmp_path)
    kill = KillSwitch(tmp_path / "kill.json")
    monitor = Monitor(paper_config, storage, parquet, kill, tmp_path / "daily")
    now = candles.index[-1].to_pydatetime() + timedelta(days=1, hours=2)
    storage.heartbeat("paper-trader", "ok", "test")

    checks = monitor.cycle(now)
    names = {c.name for c in checks}
    assert {"database", f"data:{SYMBOL}", "kill_switch"} <= names
    assert "service:monitor" not in names  # it does not vouch for itself
    assert len(storage.service_checks(now, now + timedelta(seconds=1))) == len(checks)
    assert any(b.service == "monitor" for b in storage.heartbeats())
    assert (tmp_path / "daily" / f"{now.date().isoformat()}.md").is_file()
    assert (tmp_path / "daily" / f"{(now - timedelta(days=2)).date().isoformat()}.json").is_file()

    KillSwitch(tmp_path / "kill.json").activate("operator stop", source="operator")
    for minute in (5, 10, 15):
        monitor.cycle(now + timedelta(minutes=minute))
    errors = [e for e in storage.events(200) if e.component == "monitor" and e.level == "error"]
    assert len(errors) == 1  # the change is reported once, not every cycle
    assert "kill_switch" in errors[0].message
    KillSwitch(tmp_path / "kill.json").reset("fabio")
    monitor.cycle(now + timedelta(minutes=20))
    assert any("healthy again" in e.message for e in storage.events(200))

    window = availability(storage, now, now + timedelta(minutes=21))
    assert window.cycles == 5
    assert window.healthy_cycles == 2
    assert window.failing == {"kill_switch": 3}
    assert window.longest_gap == timedelta(minutes=5)


def test_daily_report_describes_the_day_from_the_database(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    run_id = session(storage, paper_config, candles, parquet, tmp_path)
    run = storage.get_run(run_id)
    assert run is not None
    fill = next(f for f in storage.fills(run_id) if f.side == "buy")
    day = fill.ts.date()
    now = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(days=1, hours=1)
    report = build_daily_report(storage, run, day, now)
    assert report["mode"] == "paper"
    assert report["candle_processed"] is True
    assert [f["side"] for f in report["fills"]] == ["buy"]
    assert report["orders"][0]["approved"] is True
    assert len(report["signals"]) == 1
    curve = storage.equity_curve(run_id)
    assert report["equity"] == pytest.approx(
        float(curve.loc[curve.index.date == day, "equity"].iloc[0])
    )
    text = render_daily_report(report)
    for part in (
        "## Account",
        "## Signals",
        "## Orders and fills",
        "## Service health",
        "simulated",
    ):
        assert part in text

    silent = build_daily_report(storage, run, day + timedelta(days=4000), now)
    assert silent["candle_processed"] is False
    assert "has not been processed" in render_daily_report(silent)


def test_soak_test_cannot_pass_before_the_required_days(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    run_id = session(storage, paper_config, candles, parquet, tmp_path)
    run = storage.get_run(run_id)
    assert run is not None
    started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=UTC)

    def healthy_cycles(until: timedelta) -> None:
        minutes = 0
        while timedelta(minutes=minutes) <= until:
            storage.add_service_checks(
                started + timedelta(minutes=minutes), [("database", True, "ok")]
            )
            minutes += 10

    healthy_cycles(timedelta(days=3))
    early = soak_status(storage, run, started + timedelta(days=3))
    assert early["verdict"] == "IN PROGRESS"  # everything healthy, and still not a pass
    assert early["elapsed_days"] == pytest.approx(3.0)

    healthy_cycles(timedelta(days=SOAK_DAYS, hours=1))
    storage.heartbeat("backup", "ok", "qts-x")
    done_at = started + timedelta(days=SOAK_DAYS, hours=1)
    done = soak_status(storage, run, done_at)
    by_name = {c["name"]: c for c in done["criteria"]}
    assert by_name["dry-run only"]["ok"]
    assert by_name["no duplicated order"]["ok"]
    assert by_name["every candle has its signals recorded"]["ok"]
    assert by_name[f"monitored for {SOAK_DAYS} days"]["ok"]
    assert done["verdict"] in {"PASSED", "FAILED"}  # a verdict exists only now

    # a monitor that went silent for two hours fails the run, even after seven days
    unhealthy = soak_status(storage, run, done_at + timedelta(hours=2))
    assert unhealthy["verdict"] == "FAILED"
    assert any("gap" in name for name in unhealthy["failing_now"])


def test_cli_import_csv_report_and_soak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    cfg = AppConfig.model_validate(
        {
            "mode": "paper",
            "market": {"symbols": [SYMBOL]},
            "risk": {"allowed_symbols": [SYMBOL]},
            "data": {"directory": str(tmp_path / "data"), "start": "2018-01-01"},
        }
    )
    (tmp_path / "cfg.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QTS_DATABASE_URL", f"sqlite:///{tmp_path / 'state' / 'qts.sqlite'}")
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QTS_LOG_LEVEL", "CRITICAL")
    candles = random_walk(1000, seed=7)
    runner = CliRunner()

    def feed(upto: int) -> str:
        candles.iloc[:upto].reset_index().to_csv(tmp_path / "feed.csv", index=False)
        result = runner.invoke(
            app, ["data", "import-csv", "--symbol", SYMBOL, "--file", "feed.csv", "-c", "cfg.yaml"]
        )
        assert result.exit_code == 0, result.output
        return (candles.index[upto - 1] + timedelta(days=1, hours=1)).isoformat()

    feed(800)
    assert runner.invoke(app, ["paper", "start", "-c", "cfg.yaml"]).exit_code == 0
    as_of = feed(1000)
    assert (
        "store now holds 1000 candles"
        in runner.invoke(
            app, ["data", "import-csv", "--symbol", SYMBOL, "--file", "feed.csv", "-c", "cfg.yaml"]
        ).output
    )  # importing the same rows again changes nothing
    step = runner.invoke(app, ["paper", "step", "--no-refresh", "--as-of", as_of, "-c", "cfg.yaml"])
    assert step.exit_code == 0, step.output
    assert '"bars_processed": "200"' in step.output

    day = candles.index[900].date().isoformat()
    daily = runner.invoke(
        app, ["report", "daily", "--date", day, "--output", "out", "-c", "cfg.yaml"]
    )
    assert daily.exit_code == 0, daily.output
    assert f"daily report {day}" in daily.output
    assert (tmp_path / "out" / f"{day}.json").is_file()

    soak = runner.invoke(app, ["report", "soak", "-c", "cfg.yaml"])
    assert soak.exit_code == 2  # in progress: explicitly not a pass
    assert "IN PROGRESS" in soak.output
    assert "this is not a pass" in soak.output

    bad = runner.invoke(
        app, ["data", "import-csv", "--symbol", "DOGE/USDT", "--file", "feed.csv", "-c", "cfg.yaml"]
    )
    assert bad.exit_code == 1
    assert (
        runner.invoke(app, ["paper", "step", "--as-of", "yesterday", "-c", "cfg.yaml"]).exit_code
        == 1
    )
    assert runner.invoke(app, ["monitor", "once", "-c", "cfg.yaml"]).exit_code in (0, 1)
