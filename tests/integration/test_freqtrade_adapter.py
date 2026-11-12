"""Freqtrade bridge (always tested) and adapter strategy (when freqtrade is installed)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from qts.core.config import AppConfig
from qts.core.errors import LiveTradingDisabledError
from qts.core.types import OrderReason, TradingMode
from qts.integrations.db.storage import Storage
from qts.integrations.freqtrade.bridge import FreqtradeBridge, exit_reason, resolve_mode
from qts.risk.kill_switch import KillSwitch
from qts.strategies import TrendFollowingStrategy
from tests.conftest import random_walk

ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "TEST/USDT"


@pytest.fixture
def bridge(config: AppConfig) -> FreqtradeBridge:
    return FreqtradeBridge(config, TradingMode.PAPER)


def freqtrade_frame() -> pd.DataFrame:
    candles = random_walk(1500, seed=7)
    return candles.reset_index().rename(columns={"timestamp": "date"})


@pytest.mark.parametrize(
    ("runmode", "dry_run", "expected"),
    [
        ("backtest", True, TradingMode.BACKTEST),
        ("hyperopt", True, TradingMode.BACKTEST),
        ("dry_run", True, TradingMode.PAPER),
        ("dry_run", False, TradingMode.LIVE),
        ("live", False, TradingMode.LIVE),
        ("live", True, TradingMode.LIVE),
        ("something_new", True, TradingMode.LIVE),  # unknown modes fail closed
    ],
)
def test_runmode_mapping_fails_closed(runmode: str, dry_run: bool, expected: TradingMode) -> None:
    assert resolve_mode(runmode, dry_run) is expected


def test_bridge_signals_match_platform_strategy(bridge: FreqtradeBridge) -> None:
    frame = freqtrade_frame()
    signals = bridge.signals(frame)
    assert signals.index.equals(frame.index)
    expected = TrendFollowingStrategy(bridge.config.strategy.params).generate_signals(
        random_walk(1500, seed=7)
    )
    assert signals["qts_entry"].tolist() == expected["entry"].tolist()
    assert signals["qts_exit"].tolist() == expected["exit"].tolist()
    assert signals["qts_entry"].any()


def test_bridge_sizing_and_risk_veto(bridge: FreqtradeBridge) -> None:
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()
    stake = bridge.stake_amount(equity=10_000, cash=10_000, price=100.0, stop_distance=10.0)
    assert stake == pytest.approx(1_000.0)  # 1% risk -> 10 units at 100
    common = {
        "pair": SYMBOL,
        "rate": 100.0,
        "stop_distance": 10.0,
        "now": now,
        "cash": 10_000.0,
        "positions": {},
        "prices": {SYMBOL: 100.0},
        "last_candle_open": now - timedelta(days=1),
    }
    assert bridge.approve_entry(amount=10.0, **common)
    assert not bridge.approve_entry(amount=30.0, **common)  # 3% risk > 1.25% limit
    assert not bridge.approve_entry(amount=10.0, **{**common, "pair": "DOGE/USDT"})
    stale = {**common, "last_candle_open": now - timedelta(days=5)}
    assert not bridge.approve_entry(amount=10.0, **stale)
    no_stop = {**common, "stop_distance": float("nan")}
    assert not bridge.approve_entry(amount=10.0, **no_stop)
    bridge.risk.kill_switch.activate("test", "operator", at=now - timedelta(hours=1))
    assert not bridge.approve_entry(amount=10.0, **common)


def test_bridge_in_live_mode_rejects_every_entry(config: AppConfig) -> None:
    live = FreqtradeBridge(config, TradingMode.LIVE, KillSwitch())
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()
    assert not live.approve_entry(
        pair=SYMBOL,
        amount=1.0,
        rate=100.0,
        stop_distance=10.0,
        now=now,
        cash=10_000.0,
        positions={},
        prices={SYMBOL: 100.0},
        last_candle_open=now - timedelta(days=1),
    )


@pytest.mark.parametrize(("runmode", "dry_run"), [("live", False), ("dry_run", False), ("x", True)])
def test_bridge_refuses_to_start_for_real_trading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runmode: str, dry_run: bool
) -> None:
    monkeypatch.setenv("QTS_CONFIG_FILE", str(ROOT / "configs" / "paper.yaml"))
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path))
    with pytest.raises(LiveTradingDisabledError):
        FreqtradeBridge.from_environment(runmode, dry_run)
    # even the double opt-in of the platform does not open the door
    monkeypatch.setenv("QTS_ENABLE_LIVE_TRADING", "true")
    monkeypatch.setenv("QTS_LIVE_TRADING_CONFIRMATION", "I-ACCEPT-REAL-MONEY-RISK")
    with pytest.raises(LiveTradingDisabledError):
        FreqtradeBridge.from_environment(runmode, dry_run)


def _entry(bridge: FreqtradeBridge, now: datetime, cash: float = 10_000.0) -> bool:
    return bridge.approve_entry(
        pair=SYMBOL,
        amount=1.0,
        rate=100.0,
        stop_distance=10.0,
        now=now,
        cash=cash,
        positions={},
        prices={SYMBOL: 100.0},
        last_candle_open=now - timedelta(days=1),
    )


def test_bot_restart_does_not_reset_the_circuit_breakers(config: AppConfig, tmp_path: Path) -> None:
    """Drawdown peak and daily halt live in a state file next to the kill switch."""
    state = tmp_path / "freqtrade_risk_state.json"
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()

    first = FreqtradeBridge(config, TradingMode.PAPER, KillSwitch(), state)
    first.on_equity(now - timedelta(days=1), 10_000.0)
    first.on_equity(now, 9_300.0)  # -7% in a day: entries halted
    assert first.risk.daily_halt
    assert not _entry(first, now, cash=9_300.0)

    restarted = FreqtradeBridge(config, TradingMode.PAPER, KillSwitch(), state)
    assert restarted.risk.daily_halt
    assert restarted.risk.drawdown == pytest.approx(0.07)
    assert not _entry(restarted, now, cash=9_300.0)

    fresh = FreqtradeBridge(config, TradingMode.PAPER, KillSwitch())  # no state file: forgets
    assert _entry(fresh, now, cash=9_300.0)


def test_unreadable_risk_state_fails_closed(config: AppConfig, tmp_path: Path) -> None:
    state = tmp_path / "freqtrade_risk_state.json"
    state.write_text("{broken")
    bridge = FreqtradeBridge(config, TradingMode.PAPER, KillSwitch(), state)
    assert bridge.risk.kill_switch.active
    assert not _entry(bridge, pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime())


# ------------------------------------------------------------------- exits
@pytest.mark.parametrize(
    ("freqtrade_reason", "expected"),
    [
        ("exit_signal", OrderReason.EXIT_SIGNAL),
        ("stop_loss", OrderReason.STOP_LOSS),
        ("stoploss_on_exchange", OrderReason.STOP_LOSS),
        ("trailing_stop_loss", OrderReason.TRAILING_STOP),
        ("roi", OrderReason.TAKE_PROFIT),
        ("partial_exit", OrderReason.EXIT_SIGNAL),
        ("force_exit", OrderReason.MANUAL),
        ("emergency_exit", OrderReason.EMERGENCY),
        ("kill_switch", OrderReason.KILL_SWITCH),
        ("some_future_reason", OrderReason.MANUAL),
        (None, OrderReason.MANUAL),
    ],
)
def test_freqtrade_exit_reasons_map_to_platform_reasons(
    freqtrade_reason: str | None, expected: OrderReason
) -> None:
    assert exit_reason(freqtrade_reason) is expected
    assert exit_reason(freqtrade_reason) is not OrderReason.ENTRY


@pytest.mark.parametrize(
    "reason", ["exit_signal", "stop_loss", "trailing_stop_loss", "roi", "force_exit", "kill_switch"]
)
def test_exits_are_authorised_and_journaled_even_with_everything_halted(
    bridge: FreqtradeBridge, reason: str
) -> None:
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()
    bridge.on_equity(now - timedelta(days=1), 10_000.0)
    bridge.on_equity(now, 9_000.0)  # daily halt
    bridge.risk.kill_switch.activate("operator stop", "operator")
    assert not _entry(bridge, now, cash=9_000.0)  # new exposure is blocked ...

    before = len(bridge.risk.journal.decisions)
    assert bridge.authorize_exit(SYMBOL, 1.5, 95.0, reason, now, held=1.5)  # ... closing is not
    assert bridge.authorize_exit(SYMBOL, 0.5, 95.0, reason, now, held=1.5)  # partial
    journaled = bridge.risk.journal.decisions[before:]
    assert [d.approved for d in journaled] == [True, True]
    assert journaled[0].order.reason is exit_reason(reason)


def test_an_exit_larger_than_the_position_is_not_a_reduction(bridge: FreqtradeBridge) -> None:
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()
    assert not bridge.authorize_exit(SYMBOL, 2.0, 95.0, "exit_signal", now, held=1.5)
    assert not bridge.authorize_exit(SYMBOL, 1.0, 95.0, "exit_signal", now, held=0.0)


def test_exit_authorisation_fails_open_on_internal_errors(
    bridge: FreqtradeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug or outage on our side must never keep the bot in a position."""
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("risk engine exploded")

    monkeypatch.setattr(bridge.risk, "evaluate", boom)
    assert bridge.authorize_exit(SYMBOL, 1.0, 95.0, "stop_loss", now, held=1.0)
    with pytest.raises(RuntimeError):  # while the entry path lets the error surface: no entry
        _entry(bridge, now)


def test_a_failing_journal_database_blocks_neither_exits_nor_the_bot(
    config: AppConfig,
) -> None:
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()

    def dead_sink(*_a: object, **_k: object) -> None:
        raise ConnectionError("database is gone")

    bridge = FreqtradeBridge(
        config, TradingMode.PAPER, KillSwitch(), None, dead_sink, dead_sink, dead_sink
    )
    assert bridge.authorize_exit(SYMBOL, 1.0, 95.0, "stop_loss", now, held=1.0)
    assert _entry(bridge, now)  # the decision itself is still taken (and logged)
    bridge.report_error(now, "exchange timeout")  # event sink fails too: swallowed
    bridge.beat(now)


def test_repeated_exchange_errors_trip_the_kill_switch_and_exits_still_pass(
    config: AppConfig,
) -> None:
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()
    bridge = FreqtradeBridge(config, TradingMode.PAPER, KillSwitch())
    for _ in range(config.risk.max_consecutive_errors - 1):
        bridge.report_error(now, "exchange timeout")
    bridge.report_healthy()  # one good loop in between: counter resets
    assert _entry(bridge, now)
    for _ in range(config.risk.max_consecutive_errors):
        bridge.report_error(now, "exchange timeout")
    assert bridge.risk.kill_switch.active
    assert not _entry(bridge, now)
    assert bridge.authorize_exit(SYMBOL, 1.0, 95.0, "stop_loss", now, held=1.0)


def test_flatten_is_requested_only_when_configured(config: AppConfig) -> None:
    plain = FreqtradeBridge(config, TradingMode.PAPER, KillSwitch())
    plain.risk.kill_switch.activate("stop", "operator")
    assert not plain.flatten_requested
    flatten_config = config.model_copy(
        update={"risk": config.risk.model_copy(update={"flatten_on_kill_switch": True})}
    )
    flatten = FreqtradeBridge(flatten_config, TradingMode.PAPER, KillSwitch())
    assert not flatten.flatten_requested
    flatten.risk.kill_switch.activate("stop", "operator")
    assert flatten.flatten_requested


def test_dry_run_decisions_are_written_to_the_platform_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entries *and* exits of the bot end up in the tables Grafana reads."""
    url = f"sqlite:///{tmp_path / 'qts.sqlite'}"
    monkeypatch.setenv("QTS_CONFIG_FILE", str(ROOT / "configs" / "paper.yaml"))
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QTS_DATABASE_URL", url)
    monkeypatch.setenv("QTS_LOG_LEVEL", "CRITICAL")
    now = pd.Timestamp("2024-01-10", tz="UTC").to_pydatetime()

    bridge = FreqtradeBridge.from_environment("dry_run", True)
    common = {
        "now": now,
        "cash": 10_000.0,
        "positions": {},
        "prices": {"BTC/USDT": 100.0},
        "last_candle_open": now - timedelta(days=1),
    }
    assert bridge.approve_entry(
        pair="BTC/USDT", amount=10.0, rate=100.0, stop_distance=10.0, **common
    )
    assert not bridge.approve_entry(
        pair="BTC/USDT", amount=90.0, rate=100.0, stop_distance=10.0, **common
    )
    assert bridge.authorize_exit("BTC/USDT", 10.0, 90.0, "stop_loss", now, held=10.0)
    bridge.beat(now)

    storage = Storage(url)
    run = storage.active_run("freqtrade")
    assert run is not None
    assert run.mode == "paper"
    rows = storage.risk_decisions(run.id)
    assert [(r.side, r.approved, r.order_reason) for r in rows] == [
        ("buy", True, "entry"),
        ("buy", False, "entry"),
        ("sell", True, "stop_loss"),
    ]
    assert any(b.service == "freqtrade" and b.status == "ok" for b in storage.heartbeats())

    # a restarted bot continues the same run instead of opening a new one
    again = FreqtradeBridge.from_environment("dry_run", True)
    assert again.authorize_exit("BTC/USDT", 1.0, 90.0, "roi", now, held=1.0)
    assert storage.active_run("freqtrade").id == run.id  # type: ignore[union-attr]
    assert len(storage.risk_decisions(run.id)) == 4


def test_core_package_never_imports_freqtrade() -> None:
    """GPL isolation: only the adapter file under freqtrade/ may import freqtrade."""
    offenders = [
        str(path)
        for path in (ROOT / "src").rglob("*.py")
        if any(
            line.strip().startswith(("import freqtrade", "from freqtrade"))
            for line in path.read_text().splitlines()
        )
    ]
    assert offenders == []


@pytest.mark.freqtrade
def test_adapter_strategy_produces_platform_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not importorskip("freqtrade"): the repo's own freqtrade/ folder would satisfy it.
    RunMode = pytest.importorskip("freqtrade.enums").RunMode

    monkeypatch.setenv("QTS_CONFIG_FILE", str(ROOT / "configs" / "default.yaml"))
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QTS_LOG_LEVEL", "CRITICAL")
    module = _load_adapter()

    strategy = module.QtsTrendFollowing(
        {"runmode": RunMode.BACKTEST, "dry_run": True, "stake_currency": "USDT"}
    )
    assert strategy.can_short is False
    assert strategy.use_custom_stoploss is True
    assert strategy.bridge.mode is TradingMode.BACKTEST
    assert strategy.startup_candle_count == 200

    frame = freqtrade_frame()
    out = strategy.populate_indicators(frame.copy(), {"pair": "BTC/USDT"})
    out = strategy.populate_entry_trend(out, {"pair": "BTC/USDT"})
    out = strategy.populate_exit_trend(out, {"pair": "BTC/USDT"})
    assert (out["enter_long"] == 1).tolist() == out["qts_entry"].tolist()
    assert (out["exit_long"] == 1).tolist() == out["qts_exit"].tolist()
    assert out["qts_entry"].sum() > 0

    # Real trading: the strategy refuses to load, so the bot cannot start at all.
    for live_config in (
        {"runmode": RunMode.LIVE, "dry_run": False, "stake_currency": "USDT"},
        {"runmode": RunMode.DRY_RUN, "dry_run": False, "stake_currency": "USDT"},
    ):
        with pytest.raises(LiveTradingDisabledError, match="live trading is not implemented"):
            module.QtsTrendFollowing(live_config)


def _load_adapter() -> Any:
    path = ROOT / "freqtrade" / "user_data" / "strategies" / "qts_trend_following.py"
    spec = importlib.util.spec_from_file_location("qts_trend_following", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeTrade:
    """The slice of freqtrade.persistence.Trade the adapter touches. ``custom``
    stands for the trade_custom_data table, i.e. what survives a bot restart."""

    entry_side = "buy"

    def __init__(self, pair: str, open_rate: float, opened: datetime, amount: float = 1.0) -> None:
        self.pair, self.open_rate, self.open_date_utc, self.amount = pair, open_rate, opened, amount
        self.custom: dict[str, Any] = {}

    def set_custom_data(self, key: str, value: Any) -> None:
        self.custom[key] = value

    def get_custom_data(self, key: str, default: Any = None) -> Any:
        return self.custom.get(key, default)


class FakeProvider:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def get_analyzed_dataframe(self, pair: str, timeframe: str) -> tuple[pd.DataFrame, None]:
        return self.frame, None


class Filled:
    ft_order_side = "buy"


class PartialSell:
    ft_order_side = "sell"
    safe_filled = 0.4
    safe_price = 95.0


@pytest.mark.freqtrade
def test_adapter_exit_stop_and_restart_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    RunMode = pytest.importorskip("freqtrade.enums").RunMode
    monkeypatch.setenv("QTS_CONFIG_FILE", str(ROOT / "configs" / "paper.yaml"))
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QTS_LOG_LEVEL", "CRITICAL")
    monkeypatch.delenv("QTS_DATABASE_URL", raising=False)
    module = _load_adapter()
    config = {"runmode": RunMode.DRY_RUN, "dry_run": True, "stake_currency": "USDT"}

    strategy = module.QtsTrendFollowing(config)
    analyzed = strategy.populate_indicators(freqtrade_frame(), {"pair": "BTC/USDT"})
    strategy.dp = FakeProvider(analyzed)
    signal = analyzed.iloc[-2]  # candle that closed before the entry
    opened = (analyzed["date"].iloc[-1] + pd.Timedelta(hours=1)).to_pydatetime()
    now = opened + timedelta(hours=1)
    trade = FakeTrade("BTC/USDT", float(signal["close"]), opened)

    # --- the stop is fixed when the entry fills and stored with the trade
    strategy.order_filled("BTC/USDT", trade, Filled(), now)
    expected_stop = trade.open_rate - float(signal["qts_stop_distance"])
    assert trade.custom[module.STOP_KEY] == pytest.approx(expected_stop)
    relative = strategy.custom_stoploss("BTC/USDT", trade, now, trade.open_rate, 0.0, False)
    assert trade.open_rate * (1 - relative) == pytest.approx(expected_stop)

    # --- restart with the entry candle no longer in memory: same stop, from the database
    restarted = module.QtsTrendFollowing(config)
    restarted.dp = FakeProvider(analyzed.iloc[0:0])
    again = restarted.custom_stoploss("BTC/USDT", trade, now, trade.open_rate, 0.0, False)
    assert again == pytest.approx(relative)

    # --- a position whose stop is unknown is an anomaly: counted, never silently ignored
    orphan = FakeTrade("BTC/USDT", 100.0, opened)
    for _ in range(restarted.bridge.config.risk.max_consecutive_errors):
        assert restarted.custom_stoploss("BTC/USDT", orphan, now, 100.0, 0.0, False) is None
    assert restarted.bridge.risk.kill_switch.active

    # --- kill switch active (shared file): no entries, exits always confirmed
    assert strategy.bridge.risk.kill_switch.active  # seen through the state file
    assert (
        strategy.confirm_trade_entry("BTC/USDT", "market", 0.01, 100.0, "GTC", now, None, "long")
        is False
    )
    for reason in ("stop_loss", "exit_signal", "force_exit", "roi", "trailing_stop_loss"):
        assert strategy.confirm_trade_exit(
            "BTC/USDT", trade, "market", 1.0, 90.0, "GTC", reason, now
        )
        assert strategy.confirm_trade_exit(
            "BTC/USDT", trade, "market", 0.4, 90.0, "GTC", reason, now
        )
    sells = [d for d in strategy.bridge.risk.journal.decisions if d.order.side.value == "sell"]
    assert len(sells) == 10
    assert all(d.approved for d in sells)
    assert (
        strategy.custom_exit("BTC/USDT", trade, now, 90.0, -0.1) is None
    )  # flatten not configured

    # --- a partial exit (Freqtrade skips the confirmation callback) is journaled on fill
    trade.is_open = True  # type: ignore[attr-defined]
    trade.amount = 0.6  # what is left after 0.4 was sold
    strategy.order_filled("BTC/USDT", trade, PartialSell(), now)
    partial = strategy.bridge.risk.journal.decisions[-1]
    assert (partial.order.side.value, partial.order.quantity, partial.approved) == (
        "sell",
        0.4,
        True,
    )
    trade.is_open = False  # type: ignore[attr-defined]
    strategy.order_filled("BTC/USDT", trade, PartialSell(), now)  # closing fill: already journaled
    assert strategy.bridge.risk.journal.decisions[-1] is partial

    # --- an internal failure: entries fail closed, exits fail open
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("wallet backend down")

    monkeypatch.setattr(strategy, "_equity_and_cash", boom)
    assert (
        strategy.confirm_trade_entry("BTC/USDT", "market", 0.01, 100.0, "GTC", now, None, "long")
        is False
    )
    monkeypatch.setattr(strategy.bridge, "authorize_exit", boom)
    assert (
        strategy.confirm_trade_exit("BTC/USDT", trade, "market", 1.0, 90.0, "GTC", "stop_loss", now)
        is True
    )
    strategy.bot_loop_start(now)  # must not raise even though the wallet is unavailable
