from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from qts.backtesting.validation import run_validation
from qts.cli import app
from qts.core.config import AppConfig
from qts.integrations.db.storage import Storage
from qts.market_data.freqtrade_export import export_to_freqtrade
from qts.market_data.store import ParquetStore
from qts.monitoring.health import run_health_checks
from qts.reporting.report import render_markdown, validation_to_dict, write_validation_report
from qts.risk.kill_switch import KillSwitch
from tests.conftest import random_walk

SYMBOL = "TEST/USDT"
runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated project directory with config, data and a SQLite database."""
    raw = {
        "mode": "backtest",
        "market": {"symbols": [SYMBOL]},
        "data": {"directory": str(tmp_path / "data")},
        "risk": {"allowed_symbols": [SYMBOL]},
        "validation": {
            "walk_forward_train_days": 400,
            "walk_forward_test_days": 150,
            "monte_carlo_simulations": 100,
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    (tmp_path / "paper.yaml").write_text(yaml.safe_dump({**raw, "mode": "paper"}))
    (tmp_path / "live.yaml").write_text(yaml.safe_dump({**raw, "mode": "live"}))
    ParquetStore(tmp_path / "data").write("binance", SYMBOL, "1d", random_walk(1500, seed=7))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QTS_DATABASE_URL", f"sqlite:///{tmp_path / 'state' / 'qts.sqlite'}")
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QTS_LOG_LEVEL", "CRITICAL")
    monkeypatch.delenv("QTS_ENABLE_LIVE_TRADING", raising=False)
    return tmp_path


def test_cli_backtest_saves_a_run(workspace: Path) -> None:
    result = runner.invoke(app, ["backtest", "-c", "config.yaml"])
    assert result.exit_code == 0, result.output
    assert "Sharpe ratio" in result.output
    assert "Buy & Hold" in result.output
    assert "Saved as run" in result.output


def test_cli_validate_writes_a_reproducible_bundle(workspace: Path) -> None:
    result = runner.invoke(app, ["validate", "-c", "config.yaml", "--no-tearsheet"])
    assert result.exit_code == 0, result.output
    (bundle,) = (workspace / "reports").iterdir()
    assert {p.name for p in bundle.iterdir()} >= {
        "report.json",
        "report.md",
        "config.yaml",
        "equity.csv",
        "trades.csv",
    }
    report = json.loads((bundle / "report.json").read_text())
    saved_config = AppConfig.model_validate(yaml.safe_load((bundle / "config.yaml").read_text()))
    assert saved_config.fingerprint() == report["config_fingerprint"]
    assert bundle.name.endswith(report["config_fingerprint"])
    assert set(report["periods"]) == {"full", "in_sample", "out_of_sample"}
    assert "survivorship" in (bundle / "report.md").read_text()


def test_cli_refuses_live_mode_even_with_opt_in(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(app, ["backtest", "-c", "live.yaml"]).exit_code == 1
    monkeypatch.setenv("QTS_ENABLE_LIVE_TRADING", "true")
    monkeypatch.setenv("QTS_LIVE_TRADING_CONFIRMATION", "I-ACCEPT-REAL-MONEY-RISK")
    for command in (["backtest"], ["paper", "start"], ["paper", "step"], ["config"]):
        result = runner.invoke(app, [*command, "-c", "live.yaml"])
        assert result.exit_code == 1
        assert "live trading is not implemented" in result.output


def test_cli_reports_config_errors_without_traceback(workspace: Path) -> None:
    result = runner.invoke(app, ["backtest", "-c", "missing.yaml"])
    assert result.exit_code == 1
    assert "ERROR: configuration file not found" in result.output
    assert "Traceback" not in result.output


def test_cli_paper_lifecycle_and_kill_switch(workspace: Path) -> None:
    assert runner.invoke(app, ["paper", "step", "-c", "config.yaml"]).exit_code == 1  # wrong mode
    start = runner.invoke(app, ["paper", "start", "-c", "paper.yaml", "--from", "2021-06-01"])
    assert start.exit_code == 0, start.output
    kill = runner.invoke(app, ["risk", "kill", "-c", "paper.yaml", "--reason", "test"])
    assert kill.exit_code == 0
    assert "ACTIVE" in runner.invoke(app, ["risk", "status", "-c", "paper.yaml"]).output
    health = runner.invoke(app, ["health", "-c", "paper.yaml"])
    assert health.exit_code == 1
    assert "[FAIL] kill_switch" in health.output
    reset = runner.invoke(app, ["risk", "reset", "-c", "paper.yaml", "--operator", "tester"])
    assert reset.exit_code == 0
    assert "not active" in runner.invoke(app, ["risk", "status", "-c", "paper.yaml"]).output
    status = runner.invoke(app, ["paper", "status", "-c", "paper.yaml"])
    assert status.exit_code == 0
    assert "Run " in status.output
    assert runner.invoke(app, ["paper", "stop", "-c", "paper.yaml"]).exit_code == 0


def test_health_checks(workspace: Path) -> None:
    config = AppConfig.model_validate(yaml.safe_load((workspace / "config.yaml").read_text()))
    storage = Storage(f"sqlite:///{workspace / 'h.sqlite'}")
    storage.init_schema()
    store = ParquetStore(workspace / "data")
    last = store.read("binance", SYMBOL, "1d").index[-1].to_pydatetime()
    fresh = last + timedelta(days=1, hours=2)
    storage.heartbeat("paper-trader", "ok")
    checks = {c.name: c for c in run_health_checks(config, storage, store, KillSwitch(), now=fresh)}
    assert checks["database"].ok
    assert checks[f"data:{SYMBOL}"].ok
    assert checks["kill_switch"].ok
    stale = {
        c.name: c
        for c in run_health_checks(
            config, storage, store, KillSwitch(), now=datetime.now(tz=UTC) + timedelta(days=3)
        )
    }
    assert not stale[f"data:{SYMBOL}"].ok
    assert not stale["service:paper-trader"].ok
    missing = config.model_copy(
        update={"market": config.market.model_copy(update={"symbols": ("NOPE/USDT",)})}
    )
    assert not run_health_checks(missing, storage, store, KillSwitch(), now=fresh)[1].ok


def test_report_rendering_and_freqtrade_export(workspace: Path) -> None:
    config = AppConfig.model_validate(yaml.safe_load((workspace / "config.yaml").read_text()))
    store = ParquetStore(workspace / "data")
    data = {SYMBOL: store.read("binance", SYMBOL, "1d")}
    report = run_validation(config, data)
    payload = validation_to_dict(report)
    json.dumps(payload)  # strictly JSON-serialisable
    markdown = render_markdown(report)
    for section in ("Out Of Sample", "Walk-forward analysis", "Monte Carlo", "## Warnings"):
        assert section in markdown
    directory = write_validation_report(report, workspace / "out", tearsheet=False)
    equity = pd.read_csv(directory / "equity.csv", index_col=0)
    assert equity["equity"].tolist() == pytest.approx(report.full.result.equity.tolist())

    (path,) = export_to_freqtrade(config, store, workspace / "ft")
    assert path.name == "TEST_USDT-1d.feather"
    exported = pd.read_feather(path)
    assert list(exported.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert exported["close"].tolist() == data[SYMBOL]["close"].tolist()
