"""Freqtrade bridge (always tested) and adapter strategy (when freqtrade is installed)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from qts.core.config import AppConfig
from qts.core.types import TradingMode
from qts.integrations.freqtrade.bridge import FreqtradeBridge, resolve_mode
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
    pytest.importorskip("freqtrade")
    from freqtrade.enums import RunMode

    monkeypatch.setenv("QTS_CONFIG_FILE", str(ROOT / "configs" / "default.yaml"))
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QTS_LOG_LEVEL", "CRITICAL")
    path = ROOT / "freqtrade" / "user_data" / "strategies" / "qts_trend_following.py"
    spec = importlib.util.spec_from_file_location("qts_trend_following", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

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

    live = module.QtsTrendFollowing(
        {"runmode": RunMode.LIVE, "dry_run": False, "stake_currency": "USDT"}
    )
    assert live.bridge.mode is TradingMode.LIVE  # the risk engine will veto everything
