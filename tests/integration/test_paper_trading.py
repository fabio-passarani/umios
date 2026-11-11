"""Paper trading must reproduce the backtest exactly when fed the same candles
one step at a time — including across simulated process restarts."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from qts.backtesting.engine import run_backtest
from qts.core.config import AppConfig
from qts.core.errors import ConfigError, DataError, ExecutionError, PersistenceError
from qts.core.types import OrderReason, TradingMode
from qts.execution.broker import PaperBroker
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


def test_crash_while_saving_a_step_does_not_duplicate_orders(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database dies after the fills were written but before the resume point
    was saved. The step must leave no trace, so the retry books every order once."""
    publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    trader(paper_config, storage, parquet, tmp_path).step(
        now=publish(parquet, candles, START + 100)
    )
    fills_before = len(storage.fills(run_id))
    trades_before = len(storage.trades(run_id))
    bars_before = len(storage.equity_curve(run_id))

    now = publish(parquet, candles, len(candles))
    with monkeypatch.context() as patch:

        def crash(*_args: object, **_kwargs: object) -> None:
            raise PersistenceError("simulated outage")

        patch.setattr(storage, "save_paper_state", crash)
        with pytest.raises(PersistenceError, match="simulated outage"):
            trader(paper_config, storage, parquet, tmp_path).step(now=now)

    # nothing of the failed step was kept
    assert len(storage.fills(run_id)) == fills_before
    assert len(storage.trades(run_id)) == trades_before
    assert len(storage.equity_curve(run_id)) == bars_before

    trader(paper_config, storage, parquet, tmp_path).step(now=now)  # the service retries
    expected = run_backtest(
        paper_config.model_copy(update={"mode": TradingMode.BACKTEST}),
        {SYMBOL: candles},
        start=candles.index[START],
    )
    assert len(expected.fills) > fills_before  # the failed step did contain orders
    assert len(storage.fills(run_id)) == len(expected.fills)
    assert len(storage.trades(run_id)) == len(expected.trades)
    assert len(storage.risk_decisions(run_id)) == len(expected.decisions)
    order_ids = [f.order_id for f in storage.fills(run_id)]
    assert len(order_ids) == len(set(order_ids))
    assert storage.equity_curve(run_id)["equity"].tolist() == pytest.approx(
        expected.equity.tolist(), rel=1e-12
    )


def test_operator_kill_stops_a_paper_trader_that_is_already_running(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    """One long-lived PaperTrader (the `qts paper run` service); the operator
    runs `qts risk kill` in another process, at wall-clock time."""
    publish(parquet, candles, START)
    service = trader(paper_config, storage, parquet, tmp_path)
    run_id = service.start()
    service.step(now=publish(parquet, candles, START + 30))
    entries_before = sum(1 for f in storage.fills(run_id) if f.side == "buy")

    KillSwitch(tmp_path / "kill.json").activate("manual stop", source="operator")

    report = service.step(now=publish(parquet, candles, len(candles)))
    assert report.kill_switch_active
    assert sum(1 for f in storage.fills(run_id) if f.side == "buy") == entries_before
    cutoff = candles.index[START + 30].tz_localize(None)  # SQLite returns naive UTC datetimes
    late = [d for d in storage.risk_decisions(run_id) if d.ts.replace(tzinfo=None) > cutoff]
    assert [d for d in late if d.side == "buy"]  # the strategy kept asking
    assert all("kill_switch" in d.failed_checks for d in late if d.side == "buy")


def _first_open_position(
    storage: Storage, cfg: AppConfig, candles: pd.DataFrame, parquet: ParquetStore, tmp_path: Path
) -> tuple[str, int]:
    """Advance a fresh session one candle at a time until a position is open."""
    publish(parquet, candles, START)
    run_id = trader(cfg, storage, parquet, tmp_path).start()
    for upto in range(START + 1, len(candles)):
        trader(cfg, storage, parquet, tmp_path).step(now=publish(parquet, candles, upto))
        if storage.positions(run_id):
            return run_id, upto
    raise AssertionError("the synthetic data never opened a position")


def test_operator_can_close_a_position_while_the_kill_switch_is_active(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    run_id, upto = _first_open_position(storage, paper_config, candles, parquet, tmp_path)
    KillSwitch(tmp_path / "kill.json").activate("manual stop", source="operator")

    # half now (manual), the rest as an emergency on the following day
    operator = trader(paper_config, storage, parquet, tmp_path)
    assert operator.request_exit([SYMBOL], OrderReason.MANUAL, fraction=0.5) == [SYMBOL]
    trader(paper_config, storage, parquet, tmp_path).step(now=publish(parquet, candles, upto + 1))
    (half,) = storage.positions(run_id)
    assert operator.request_exit(None, OrderReason.EMERGENCY) == [SYMBOL]
    trader(paper_config, storage, parquet, tmp_path).step(now=publish(parquet, candles, upto + 2))

    assert storage.positions(run_id) == []
    sells = [f for f in storage.fills(run_id) if f.side == "sell"]
    assert [f.reason for f in sells] == ["manual", "emergency"]
    assert sells[0].quantity == pytest.approx(half.quantity)  # half out, half left
    assert sells[0].ts.replace(tzinfo=None) == candles.index[upto].tz_localize(None)  # next open
    exits = [d for d in storage.risk_decisions(run_id) if d.side == "sell"]
    assert len(exits) == 2
    assert all(d.approved for d in exits)
    assert operator.request_exit(None) == []  # nothing left to close


def test_broker_failure_in_the_middle_of_a_step_leaves_no_trace_and_is_counted(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exchange rejects an order while a position is open. Nothing of the
    half-processed step is stored; the failure counts towards the kill switch;
    the retry executes every order exactly once."""
    run_id, upto = _first_open_position(storage, paper_config, candles, parquet, tmp_path)
    fills_before = len(storage.fills(run_id))
    now = publish(parquet, candles, len(candles))

    def reject(*_a: object, **_k: object) -> None:
        raise ExecutionError("exchange rejected the order")

    with monkeypatch.context() as patch:
        patch.setattr(PaperBroker, "execute", reject)
        with pytest.raises(ExecutionError):
            trader(paper_config, storage, parquet, tmp_path).step(now=now)
    assert len(storage.fills(run_id)) == fills_before
    assert len(storage.equity_curve(run_id)) == upto - START  # no bar of the failed step
    assert len(storage.positions(run_id)) == 1  # the open position is still on the books
    assert any(b.status == "degraded" for b in storage.heartbeats())
    state = storage.load_paper_state(run_id)
    assert state is not None
    assert state["risk"]["consecutive_errors"] == 1

    trader(paper_config, storage, parquet, tmp_path).step(now=now)
    expected = run_backtest(
        paper_config.model_copy(update={"mode": TradingMode.BACKTEST}),
        {SYMBOL: candles},
        start=candles.index[START],
    )
    assert len(storage.fills(run_id)) == len(expected.fills)
    assert storage.equity_curve(run_id)["equity"].tolist() == pytest.approx(
        expected.equity.tolist(), rel=1e-12
    )
    state = storage.load_paper_state(run_id)
    assert state is not None
    assert state["risk"]["consecutive_errors"] == 0  # a healthy step resets the count
