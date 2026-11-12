"""Things that go wrong around a paper-trading session. In every case the rule is
the same: no order booked twice, no exposure the risk engine did not approve,
and the session ends where an undisturbed one would have."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest

from qts.backtesting.engine import run_backtest
from qts.core.config import AppConfig
from qts.core.errors import DataError, PersistenceError
from qts.core.types import TradingMode
from qts.execution.paper import PaperTrader
from qts.integrations.db.storage import Storage
from qts.market_data.sources import BinanceVisionSource
from qts.market_data.sources.base import MarketDataSource
from qts.market_data.store import ParquetStore
from qts.risk.kill_switch import KillSwitch
from tests.integration.conftest import SYMBOL
from tests.integration.test_paper_trading import START, publish, trader


def reference(cfg: AppConfig, candles: pd.DataFrame) -> object:
    return run_backtest(
        cfg.model_copy(update={"mode": TradingMode.BACKTEST}),
        {SYMBOL: candles},
        start=candles.index[START],
    )


def assert_matches(storage: Storage, run_id: str, expected: object) -> None:
    fills = storage.fills(run_id)
    assert len(fills) == len(expected.fills) > 0  # type: ignore[attr-defined]
    assert len({f.order_id for f in fills}) == len(fills)  # no order booked twice
    assert storage.equity_curve(run_id)["equity"].tolist() == pytest.approx(
        expected.equity.tolist(),  # type: ignore[attr-defined]
        rel=1e-12,
    )
    assert all(d.approved for d in storage.risk_decisions(run_id) if d.side == "sell")
    buys = {f.order_id for f in fills if f.side == "buy"}
    approved = {d.order_id for d in storage.risk_decisions(run_id) if d.approved}
    assert buys <= approved  # every entry that exists was approved by the risk engine


# ------------------------------------------------------------------------- data
def test_duplicate_candles_change_nothing(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    for upto in (START + 80, START + 80, START + 200, len(candles)):
        now = publish(parquet, candles, upto)
        # the same history delivered again, plus an overlapping tail delivered twice
        parquet.write("binance", SYMBOL, "1d", candles.iloc[:upto])
        parquet.write("binance", SYMBOL, "1d", candles.iloc[max(0, upto - 40) : upto])
        trader(paper_config, storage, parquet, tmp_path).step(now=now)
        trader(paper_config, storage, parquet, tmp_path).step(now=now)
    assert_matches(storage, run_id, reference(paper_config, candles))


def test_missing_candles_are_skipped_never_invented(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    holes = candles.drop(candles.index[[START + 30, START + 31, START + 150]])
    publish(parquet, holes, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    for upto in (START + 100, START + 260, len(holes)):
        trader(paper_config, storage, parquet, tmp_path).step(now=publish(parquet, holes, upto))
    assert_matches(storage, run_id, reference(paper_config, holes))
    stored_days = set(storage.equity_curve(run_id).index)
    assert not stored_days & set(candles.index[[START + 30, START + 31, START + 150]])


def test_late_data_is_an_error_then_the_backlog_is_processed_once(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    now = publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    with pytest.raises(DataError, match="stale"):  # four days without a new candle
        trader(paper_config, storage, parquet, tmp_path).step(now=now + timedelta(days=4))
    assert storage.equity_curve(run_id).empty  # nothing traded on stale data
    assert storage.load_paper_state(run_id)["risk"]["consecutive_errors"] == 1  # type: ignore[index]

    # the feed comes back with everything it missed
    report = trader(paper_config, storage, parquet, tmp_path).step(
        now=publish(parquet, candles, len(candles))
    )
    assert report.bars_processed == len(candles) - START
    assert not report.kill_switch_active  # one late feed is not three
    assert_matches(storage, run_id, reference(paper_config, candles))


class FlakySource(MarketDataSource):
    """Fails like a dropped connection for the first ``failures`` calls."""

    name = "flaky"

    def __init__(self, candles: pd.DataFrame, failures: int) -> None:
        self.candles, self.failures, self.calls = candles, failures, 0

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
        self.calls += 1
        if self.calls <= self.failures:
            raise DataError("download failed: connection reset by peer")
        return self.candles


def test_connection_loss_is_counted_and_trading_resumes(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    now = publish(parquet, candles, START)
    kill = KillSwitch(tmp_path / "kill.json")
    PaperTrader(paper_config, storage, parquet, kill).start()
    run = storage.active_paper_run()
    assert run is not None
    source = FlakySource(candles, failures=2)
    later = candles.index[-1].to_pydatetime() + timedelta(days=1, hours=1)
    for _ in range(2):
        with pytest.raises(DataError, match="connection reset"):
            PaperTrader(paper_config, storage, parquet, kill, source).step(now=later)
    assert not kill.active  # two failures: below the limit of three
    assert any(b.status == "degraded" for b in storage.heartbeats())
    assert storage.equity_curve(run.id).empty

    PaperTrader(paper_config, storage, parquet, kill, source).step(now=later)  # connection back
    assert_matches(storage, run.id, reference(paper_config, candles))
    assert any(b.status == "ok" for b in storage.heartbeats())
    assert now < later


def test_http_failures_of_the_real_source_surface_as_data_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable", request=request)

    source = BinanceVisionSource(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(DataError, match="download failed"):
        source.fetch_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 1, 31))

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    source = BinanceVisionSource(client=httpx.Client(transport=httpx.MockTransport(broken)))
    with pytest.raises(DataError, match="download failed"):
        source.fetch_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 1, 31))


# ------------------------------------------------------------------ persistence
@pytest.mark.parametrize(
    "stage",
    [
        "append_equity",
        "add_fills",
        "add_trades",
        "add_risk_decisions",
        "add_risk_events",
        "add_signals",
        "replace_positions",
        "save_paper_state",
        "update_metrics",
    ],
)
def test_interruption_at_any_point_of_the_write_never_duplicates_an_order(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """The database connection dies while the step is being recorded — at each
    of the nine writes in turn."""
    publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    trader(paper_config, storage, parquet, tmp_path).step(
        now=publish(parquet, candles, START + 150)
    )
    before = (
        len(storage.fills(run_id)),
        len(storage.equity_curve(run_id)),
        len(storage.signals(run_id)),
    )
    now = publish(parquet, candles, len(candles))

    def die(*_a: object, **_k: object) -> None:
        raise PersistenceError(f"connection lost during {stage}")

    with monkeypatch.context() as patch:
        patch.setattr(storage, stage, die)
        with pytest.raises(PersistenceError, match=stage):
            trader(paper_config, storage, parquet, tmp_path).step(now=now)
    after = (
        len(storage.fills(run_id)),
        len(storage.equity_curve(run_id)),
        len(storage.signals(run_id)),
    )
    assert after == before  # the interrupted step left nothing behind

    trader(paper_config, storage, parquet, tmp_path).step(now=now)
    assert_matches(storage, run_id, reference(paper_config, candles))
    assert len(storage.signals(run_id)) == len(candles) - START  # one per candle, none twice


def test_restart_with_an_open_position_resumes_with_the_same_stop(
    storage: Storage,
    paper_config: AppConfig,
    candles: pd.DataFrame,
    parquet: ParquetStore,
    tmp_path: Path,
) -> None:
    publish(parquet, candles, START)
    run_id = trader(paper_config, storage, parquet, tmp_path).start()
    upto = START
    while not storage.positions(run_id):
        upto += 1
        trader(paper_config, storage, parquet, tmp_path).step(now=publish(parquet, candles, upto))
    (position,) = storage.positions(run_id)
    state = storage.load_paper_state(run_id)
    assert state is not None
    assert state["positions"][0]["stop_price"] == pytest.approx(position.stop_price)

    # every remaining candle processed by a brand-new process
    for step in range(upto + 1, len(candles) + 1):
        trader(paper_config, storage, parquet, tmp_path).step(now=publish(parquet, candles, step))
    assert_matches(storage, run_id, reference(paper_config, candles))
