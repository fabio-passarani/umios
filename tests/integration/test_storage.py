from __future__ import annotations

import pandas as pd
import pytest

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import run_backtest
from qts.backtesting.metrics import compute_metrics
from qts.core.config import AppConfig
from qts.core.errors import PersistenceError
from qts.core.types import TradingMode
from qts.integrations.db.storage import Storage, normalize_url
from qts.reporting.persist import save_backtest
from tests.integration.conftest import SYMBOL


def test_backtest_round_trips_through_the_database(
    storage: Storage, paper_config: AppConfig, candles: pd.DataFrame
) -> None:
    config = paper_config.model_copy(update={"mode": TradingMode.BACKTEST})
    data = {SYMBOL: candles}
    result = run_backtest(config, data)
    capital = config.portfolio.initial_capital
    bench = buy_and_hold(data, capital, config.costs)
    metrics = compute_metrics(result.equity, capital, result.trades, result.exposure)
    run_id = save_backtest(storage, result, metrics, bench)

    run = storage.get_run(run_id)
    assert run is not None
    assert run.status == "completed"
    assert run.config == config.model_dump(mode="json")  # reproducibility: exact config kept
    assert run.config_fingerprint == config.fingerprint()
    assert run.metrics is not None
    assert run.metrics["sharpe"] == pytest.approx(metrics["sharpe"])

    curve = storage.equity_curve(run_id)
    assert len(curve) == len(result.equity)
    assert curve["equity"].tolist() == pytest.approx(result.equity.tolist())
    assert curve["drawdown"].min() == pytest.approx(-metrics["max_drawdown"])
    assert len(storage.trades(run_id)) == len(result.trades)
    decisions = storage.risk_decisions(run_id)
    assert len(decisions) == len(result.decisions)
    assert sum(t.pnl for t in storage.trades(run_id)) == pytest.approx(
        sum(t.pnl for t in result.trades)
    )


def test_events_heartbeats_and_state(storage: Storage, paper_config: AppConfig) -> None:
    storage.record_event("error", "test", "boom")
    storage.heartbeat("svc", "ok", "fine")
    storage.heartbeat("svc", "degraded", "slow")  # upsert, not duplicate
    assert storage.events()[0].message == "boom"
    beats = storage.heartbeats()
    assert [(b.service, b.status) for b in beats] == [("svc", "degraded")]

    run_id = storage.create_run("paper", "paper", "s", {"a": 1}, "f", 100.0)
    assert storage.active_paper_run().id == run_id  # type: ignore[union-attr]
    storage.save_paper_state(run_id, {"cash": 1.0})
    storage.save_paper_state(run_id, {"cash": 2.0})
    assert storage.load_paper_state(run_id) == {"cash": 2.0}
    storage.finish_run(run_id, "stopped", {"sharpe": float("nan"), "pf": float("inf")})
    assert storage.active_paper_run() is None
    assert storage.get_run(run_id).metrics == {"sharpe": None, "pf": None}  # type: ignore[union-attr]
    assert storage.ping()


def test_errors_are_wrapped_without_leaking_the_dsn() -> None:
    secret_url = "postgresql://qts:topsecret@127.0.0.1:1/none"
    assert normalize_url(secret_url).startswith("postgresql+psycopg://")
    storage = Storage(secret_url)
    assert storage.ping() is False
    with pytest.raises(PersistenceError) as excinfo:
        storage.init_schema()
    assert "topsecret" not in str(excinfo.value)
    with pytest.raises(PersistenceError) as excinfo:
        storage.record_event("info", "x", "y")
    assert "topsecret" not in str(excinfo.value)
