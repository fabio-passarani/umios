"""Paper trading must reproduce the backtest exactly when fed the same candles
one step at a time — including across simulated process restarts."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from qts.backtesting.engine import run_backtest
from qts.core.config import AppConfig
from qts.core.errors import ConfigError, DataError
from qts.core.types import TradingMode
from qts.execution.paper import PaperTrader
from qts.integrations.db.storage import Storage
from qts.market_data.store import ParquetStore
from qts.risk.kill_switch import KillSwitch
from tests.integration.conftest import SYMBOL

START = 700  # bar index where the paper session begins


def publish(parquet: ParquetStore, candles: pd.DataFrame, upto: int) -> pd.Timestamp:
    parquet.write("binance", SYMBOL, "1d", candles.iloc[:upto])
    return candles.index[upto - 1] + timedelta(days=1, hours=1)  # shortly after the close


def trader(config: AppConfig, storage: Storage, parquet: ParquetStore, tmp_path: Path):  # type: ignore[no-untyped-def]
    return PaperTrader(config, storage, parquet, KillSwitch(tmp_path / "kill.json"))


def test_stepwise_paper_trading_equals_backtest(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    for upto in (START + 1, START + 2, START + 60, START + 61, START + 300, len(candles)):
        now = publish(parquet, candles, upto)
        # a brand-new PaperTrader each time = a process restart between steps
        report = trader(paper_config, storage, parquet, tmp_path).step(now=now)
    assert report.last_bar == candles.index[-1].to_pydatetime()

    expected = run_backtest(
        paper_config.model_copy(update={"mode": TradingMode.BACKTEST}),
        {SYMBOL: candles},
        start=candles.index[START],
    )
    curve = storage.equity_curve(run_id)
    assert len(curve) == len(expected.equity)
    assert curve["equity"].tolist() == pytest.approx(expected.equity.tolist(), rel=1e-12)
    assert len(storage.trades(run_id)) == len(expected.trades) > 0
    assert [round(t.pnl, 6) for t in storage.trades(run_id)] == [
        round(t.pnl, 6) for t in expected.trades
    ]
    assert len(storage.risk_decisions(run_id)) == len(expected.decisions)
    assert {p.symbol for p in storage.positions(run_id)} == {
        p.symbol for p in expected.open_positions
    }
    metrics = storage.get_run(run_id).metrics  # type: ignore[union-attr]
    assert metrics["trades"] == len(expected.trades)
    assert any(b.service == "paper-trader" and b.status == "ok" for b in storage.heartbeats())


def test_step_is_idempotent_without_new_candles(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    now = publish(parquet, candles, START + 50)
    first = trader(paper_config, storage, parquet, tmp_path).step(now=now)
    again = trader(paper_config, storage, parquet, tmp_path).step(now=now)
    assert first.bars_processed == 50
    assert again.bars_processed == 0
    assert again.equity == pytest.approx(first.equity)
    assert len(storage.equity_curve(run_id)) == 50


def test_new_session_only_trades_future_candles(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    now = publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    report = trader(paper_config, storage, parquet, tmp_path).step(now=now)
    assert report.bars_processed == 0
    assert storage.equity_curve(run_id).empty


def test_kill_switch_blocks_paper_entries(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    KillSwitch(tmp_path / "kill.json").activate(
        "manual", "operator", at=candles.index[START].to_pydatetime()
    )
    now = publish(parquet, candles, len(candles))
    report = trader(paper_config, storage, parquet, tmp_path).step(now=now)
    assert report.kill_switch_active
    assert report.fills == 0
    assert report.rejected_orders > 0
    assert all("kill_switch" in d.failed_checks for d in storage.risk_decisions(run_id))
    assert (storage.equity_curve(run_id)["equity"] == 10_000.0).all()
    assert any(b.status == "halted" for b in storage.heartbeats())


def test_stale_data_is_an_error_and_repeated_errors_trip_the_kill_switch(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    now = publish(parquet, candles, START)
    trader(paper_config, storage, parquet, tmp_path).start()
    late = now + timedelta(days=5)
    for _ in range(paper_config.risk.max_consecutive_errors):
        with pytest.raises(DataError, match="stale"):
            trader(paper_config, storage, parquet, tmp_path).step(now=late)
    assert KillSwitch(tmp_path / "kill.json").active
    assert storage.events()[0].level == "error"
    assert any(b.status == "degraded" for b in storage.heartbeats())


def test_config_change_requires_a_new_session(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    now = publish(parquet, candles, START)
    trader(paper_config, storage, parquet, tmp_path).start()
    with pytest.raises(ConfigError, match="already running"):
        trader(paper_config, storage, parquet, tmp_path).start()
    changed = paper_config.model_copy(
        update={"costs": paper_config.costs.model_copy(update={"fee_rate": 0.002})}
    )
    with pytest.raises(ConfigError, match="configuration changed"):
        trader(changed, storage, parquet, tmp_path).step(now=now)
    assert trader(paper_config, storage, parquet, tmp_path).stop() is not None
    with pytest.raises(ConfigError, match="no active paper session"):
        trader(paper_config, storage, parquet, tmp_path).step(now=now)


def test_paper_trader_refuses_non_paper_mode(
    storage: Storage, paper_config: AppConfig, parquet: ParquetStore, tmp_path: Path
) -> None:
    backtest = paper_config.model_copy(update={"mode": TradingMode.BACKTEST})
    with pytest.raises(ConfigError, match="mode: paper"):
        trader(backtest, storage, parquet, tmp_path)
