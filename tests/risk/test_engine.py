"""Every limit of the risk engine, one by one."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from qts.core.types import OrderReason, TradingMode
from qts.risk import KillSwitch
from tests.risk.helpers import NOW, SYMBOL, buy, context, engine, sell


def failed(decision) -> set[str]:  # type: ignore[no-untyped-def]
    return {c.name for c in decision.failed_checks}


def test_valid_entry_is_approved_and_journaled() -> None:
    risk = engine()
    decision = risk.evaluate(buy(), context())  # risk 1%, position 10%
    assert decision.approved
    assert decision.approval is not None
    assert decision.reason == ""
    assert risk.journal.decisions == [decision]


def test_risk_per_trade_limit() -> None:
    decision = engine().evaluate(buy(quantity=30, stop=90.0), context())  # 300 = 3% > 2%
    assert not decision.approved
    assert "max_risk_per_trade" in failed(decision)
    assert decision.approval is None


def test_stop_loss_is_mandatory_and_must_be_below_entry() -> None:
    assert "stop_loss_required" in failed(engine().evaluate(buy(stop=None), context()))
    assert "stop_loss_valid" in failed(engine().evaluate(buy(stop=101.0), context()))
    assert "stop_loss_valid" in failed(engine().evaluate(buy(stop=-5.0), context()))


def test_position_size_limit() -> None:
    decision = engine().evaluate(buy(quantity=60, stop=99.0), context())  # 60% > 50%
    assert failed(decision) == {"max_position_size"}


def test_position_size_limit_counts_existing_holding() -> None:
    ctx = context(cash=6_000.0, positions={SYMBOL: 40.0})  # already 40% of equity
    decision = engine().evaluate(buy(quantity=20, stop=99.0), ctx)
    assert "max_position_size" in failed(decision)


def test_total_exposure_limit() -> None:
    risk = engine(max_total_exposure_pct=0.6, max_position_pct=0.5)
    ctx = context(cash=5_000.0, positions={"ETH/USDT": 500.0})  # 50% exposed
    decision = risk.evaluate(buy(quantity=20, stop=99.0), ctx)  # +20% -> 70%
    assert failed(decision) == {"max_total_exposure"}


def test_open_positions_limit() -> None:
    ctx = context(cash=9_000.0, positions={"ETH/USDT": 50.0, "SOL/USDT": 500.0})
    decision = engine().evaluate(buy(quantity=1), ctx)
    assert failed(decision) == {"max_open_positions"}


def test_cash_must_cover_notional_plus_costs() -> None:
    ctx = context(cash=1_000.0, positions={"ETH/USDT": 900.0})  # equity 10k, cash 1k
    exact = engine().evaluate(buy(quantity=10, stop=99.0), ctx)  # 1000 + 0.2% costs > cash
    assert failed(exact) == {"cash_available"}
    assert engine().evaluate(buy(quantity=9.9, stop=99.0), ctx).approved


def test_daily_loss_limit_halts_new_entries_then_releases() -> None:
    risk = engine()
    risk.on_equity_update(NOW - timedelta(days=2), 10_000.0)
    risk.on_equity_update(NOW - timedelta(days=1), 9_400.0)  # -6% on the day
    assert risk.daily_halt
    assert "daily_loss_limit" in failed(risk.evaluate(buy(quantity=5), context()))
    risk.on_equity_update(NOW, 9_400.0)  # flat next day: halt lifts
    assert not risk.daily_halt
    assert risk.evaluate(buy(quantity=5), context()).approved


def test_drawdown_breach_trips_kill_switch() -> None:
    risk = engine()
    risk.on_equity_update(NOW - timedelta(days=30), 10_000.0)
    for day, equity in enumerate([9_700, 9_400, 9_100, 8_800, 8_500, 8_200, 7_900]):
        risk.on_equity_update(NOW - timedelta(days=20 - day), float(equity))
    assert risk.drawdown == pytest.approx(0.21)
    assert risk.kill_switch.active
    decision = risk.evaluate(buy(quantity=1), context())
    assert {"kill_switch", "max_drawdown"} <= failed(decision)
    assert any(e.kind == "kill_switch:max_drawdown" for e in risk.journal.events)


def test_kill_switch_blocks_entries_but_allows_closing() -> None:
    risk = engine()
    risk.kill_switch.activate("manual stop", source="operator", at=NOW - timedelta(hours=1))
    assert "kill_switch" in failed(risk.evaluate(buy(), context()))
    ctx = context(positions={SYMBOL: 5.0})
    assert risk.evaluate(sell(5.0), ctx).approved  # risk-reducing: always possible


def test_kill_switch_survives_restart_and_needs_manual_reset(tmp_path: Path) -> None:
    path = tmp_path / "kill.json"
    KillSwitch(path).activate("anomaly", source="operator", at=NOW - timedelta(days=1))
    restarted = KillSwitch(path)
    assert restarted.active
    assert restarted.state.reason == "anomaly"
    risk = engine(kill_switch=restarted)
    assert not risk.evaluate(buy(), context()).approved
    with pytest.raises(ValueError, match="operator"):
        restarted.reset("  ")
    restarted.reset("fabio")
    assert not KillSwitch(path).active
    assert engine(kill_switch=KillSwitch(path)).evaluate(buy(), context()).approved


def test_corrupt_kill_switch_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "kill.json"
    path.write_text("{not json")
    assert KillSwitch(path).active


def test_kill_switch_first_reason_is_kept() -> None:
    switch = KillSwitch()
    switch.activate("first", "a", at=NOW)
    switch.activate("second", "b", at=NOW)
    assert switch.state.reason == "first"


def test_rebase_drawdown_after_reset_allows_trading_again() -> None:
    risk = engine()
    risk.on_equity_update(NOW - timedelta(days=3), 10_000.0)
    risk.on_equity_update(NOW - timedelta(days=2), 9_700.0)
    risk.on_equity_update(NOW - timedelta(days=1), 9_400.0)
    for i, equity in enumerate([9_100.0, 8_800.0, 8_500.0, 8_200.0, 7_900.0]):
        risk.on_equity_update(NOW + timedelta(days=i), equity)
    later = NOW + timedelta(days=10)
    risk.on_equity_update(later, 7_900.0)
    assert risk.kill_switch.active
    risk.kill_switch.reset("fabio")
    ctx = context(now=later, data_time=later, cash=7_900.0)
    assert "max_drawdown" in failed(risk.evaluate(buy(quantity=1), ctx))  # still vetoed
    risk.rebase_drawdown(later, "fabio")
    assert risk.evaluate(buy(quantity=1), ctx).approved


def test_stale_market_data_blocks_entries() -> None:
    ctx = context(data_time=NOW - timedelta(hours=72))
    assert "data_freshness" in failed(engine().evaluate(buy(), ctx))
    future = context(data_time=NOW + timedelta(hours=1))
    assert "data_freshness" in failed(engine().evaluate(buy(), future))


def test_abnormal_price_blocks_entries() -> None:
    decision = engine().evaluate(buy(price=140.0, stop=130.0, quantity=1), context())
    assert "price_deviation" in failed(decision)
    unknown = engine(allowed_symbols=None).evaluate(buy(symbol="NEW/USDT"), context())
    assert "price_available" in failed(unknown)


def test_symbol_whitelist() -> None:
    assert "symbol_allowed" in failed(engine().evaluate(buy(symbol="DOGE/USDT"), context()))


@pytest.mark.parametrize(
    "order",
    [
        buy(quantity=0.0),
        buy(quantity=-1.0),
        buy(quantity=float("nan")),
        buy(quantity=float("inf")),
        buy(price=0.0),
        buy(price=float("nan")),
        buy(stop=float("nan")),
    ],
)
def test_malformed_orders_are_rejected(order) -> None:  # type: ignore[no-untyped-def]
    decision = engine().evaluate(order, context())
    assert not decision.approved
    assert "order_sanity" in failed(decision)


def test_order_rate_limit() -> None:
    risk = engine()
    for _ in range(3):
        assert risk.evaluate(buy(quantity=1), context()).approved
    assert failed(risk.evaluate(buy(quantity=1), context())) == {"max_orders_per_day"}
    tomorrow = context(now=NOW + timedelta(days=1), data_time=NOW + timedelta(days=1))
    assert risk.evaluate(buy(quantity=1, created_at=tomorrow.now), tomorrow).approved


def test_repeated_errors_trip_kill_switch_and_success_resets_counter() -> None:
    risk = engine()
    risk.record_error(NOW, "timeout")
    risk.record_error(NOW, "timeout")
    risk.record_success()
    risk.record_error(NOW, "timeout")
    assert not risk.kill_switch.active
    risk.record_error(NOW, "timeout")
    risk.record_error(NOW, "timeout")
    assert risk.kill_switch.active
    assert not risk.evaluate(buy(), context()).approved


def test_invalid_equity_trips_kill_switch() -> None:
    risk = engine()
    risk.on_equity_update(NOW, float("nan"))
    assert risk.kill_switch.active


def test_live_mode_rejects_everything() -> None:
    risk = engine(mode=TradingMode.LIVE)
    assert "trading_mode" in failed(risk.evaluate(buy(), context()))
    ctx = context(positions={SYMBOL: 1.0})
    assert "trading_mode" in failed(risk.evaluate(sell(1.0), ctx))


def test_short_selling_is_impossible() -> None:
    assert "no_short_selling" in failed(engine().evaluate(sell(1.0), context()))
    ctx = context(positions={SYMBOL: 1.0})
    assert "no_short_selling" in failed(engine().evaluate(sell(1.5), ctx))


def test_buy_orders_must_be_entries() -> None:
    decision = engine().evaluate(buy(reason=OrderReason.STOP_LOSS), context())
    assert "entry_reason" in failed(decision)


def test_every_decision_is_recorded_including_rejections() -> None:
    seen = []
    risk = engine()
    risk.journal._sinks.append(seen.append)
    risk.evaluate(buy(), context())
    risk.evaluate(buy(stop=None), context())
    assert [d.approved for d in risk.journal.decisions] == [True, False]
    assert seen == risk.journal.decisions
    assert all(d.checks for d in risk.journal.decisions)


def test_snapshot_restore_preserves_circuit_breakers() -> None:
    risk = engine()
    risk.on_equity_update(NOW - timedelta(days=2), 10_000.0)
    risk.on_equity_update(NOW - timedelta(days=1), 9_400.0)
    risk.evaluate(buy(quantity=1), context(now=NOW - timedelta(days=1)))
    clone = engine()
    clone.restore(risk.snapshot())
    assert clone.daily_halt
    assert clone.drawdown == pytest.approx(risk.drawdown)
    assert clone.snapshot() == risk.snapshot()
