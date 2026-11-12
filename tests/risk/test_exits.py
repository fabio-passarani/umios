"""Authorisation model for everything that is *not* a new entry.

New exposure fails closed. Reducing or closing an existing position goes through
the reduce-only path and must never be blocked by the conditions that block
entries — while still being a real authorisation: it cannot open a short, flip
a position or pass an entry off as an exit, and it is journaled.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from qts.backtesting.engine import build_session, compute_signal_frames, iter_bars, run_backtest
from qts.core.config import AppConfig, CostConfig
from qts.core.errors import RiskViolationError
from qts.core.types import Order, OrderReason, Side, TradingMode
from qts.execution import PaperBroker
from qts.execution.session import TradingSession
from qts.portfolio import Portfolio
from qts.risk import KillSwitch, RiskEngine
from tests.backtesting.test_engine import SYMBOL as TEST_SYMBOL
from tests.backtesting.test_engine import ScriptedStrategy
from tests.conftest import make_candles
from tests.risk.helpers import NOW, SYMBOL, buy, context, engine, sell

EXIT_REASONS = [r for r in OrderReason if r is not OrderReason.ENTRY]


def failed(decision: Any) -> set[str]:
    return {c.name for c in decision.failed_checks}


# --------------------------------------------------------------- engine: reductions
def _blocked_engine() -> RiskEngine:
    """An engine in which every entry-blocking condition is true at once."""
    risk = engine(max_drawdown_pct=0.10)
    risk.on_equity_update(NOW - timedelta(days=2), 10_000.0)
    risk.on_equity_update(NOW - timedelta(days=1), 9_000.0)  # daily halt + drawdown trip
    risk.kill_switch.activate("operator stop", source="operator")
    for _ in range(5):
        risk.record_error(NOW, "exchange down")
    return risk


@pytest.mark.parametrize("reason", EXIT_REASONS)
def test_every_kind_of_exit_is_authorised_while_entries_are_blocked(reason: OrderReason) -> None:
    risk = _blocked_engine()
    assert risk.kill_switch.active
    assert risk.daily_halt
    held = context(positions={SYMBOL: 5.0})
    assert not risk.evaluate(buy(quantity=0.1), held).approved  # no new exposure

    decision = risk.evaluate(sell(5.0, reason=reason), held)
    assert decision.approved
    assert decision.approval is not None
    assert risk.journal.decisions[-1] is decision  # journaled like any order


@pytest.mark.parametrize(
    "broken",
    [
        {"cash": float("nan")},
        {"cash": -500.0},
        {"data_time": NOW - timedelta(days=30)},
        {"prices": {}},
        {"positions": {SYMBOL: 5.0, "ETH/USDT": float("nan")}},
        {"positions": {SYMBOL: 5.0, "SOL/USDT": -3.0}},
    ],
)
def test_a_broken_account_view_cannot_block_an_exit(broken: dict[str, Any]) -> None:
    """Stale data, missing prices, nonsense cash: reasons to stop entering,
    never reasons to stay in a position."""
    ctx = context(**{"positions": {SYMBOL: 5.0}, **broken})
    assert engine().evaluate(sell(5.0, reason=OrderReason.STOP_LOSS), ctx).approved


def test_exit_of_a_symbol_removed_from_the_whitelist_is_authorised() -> None:
    risk = engine(allowed_symbols=("ETH/USDT",))
    ctx = context(positions={SYMBOL: 5.0})
    assert "symbol_allowed" in failed(risk.evaluate(buy(), ctx))
    assert risk.evaluate(sell(5.0), ctx).approved


def test_partial_exit_is_authorised_and_bounded_by_the_position() -> None:
    risk = engine()
    ctx = context(positions={SYMBOL: 5.0})
    assert risk.evaluate(sell(2.0, reason=OrderReason.TAKE_PROFIT), ctx).approved
    assert risk.evaluate(sell(5.0, reason=OrderReason.MANUAL), ctx).approved
    assert "no_short_selling" in failed(risk.evaluate(sell(5.01), ctx))
    assert "no_short_selling" in failed(risk.evaluate(sell(1.0), context()))  # nothing held


def test_a_sell_cannot_be_disguised_as_an_entry_or_carry_bad_numbers() -> None:
    risk = engine()
    ctx = context(positions={SYMBOL: 5.0})
    assert "exit_reason" in failed(risk.evaluate(sell(1.0, reason=OrderReason.ENTRY), ctx))
    for quantity in (0.0, -1.0, math.nan, math.inf):
        assert not risk.evaluate(sell(quantity), ctx).approved
    bad_price = Order(SYMBOL, Side.SELL, 1.0, math.nan, OrderReason.STOP_LOSS, "t", NOW)
    assert "order_sanity" in failed(risk.evaluate(bad_price, ctx))
    assert not risk.evaluate(sell(1.0), context(positions={SYMBOL: math.nan})).approved


def test_live_mode_authorises_nothing_not_even_exits() -> None:
    """There is no live broker: nothing can be executed in that mode at all."""
    risk = engine(mode=TradingMode.LIVE)
    assert "trading_mode" in failed(risk.evaluate(sell(5.0), context(positions={SYMBOL: 5.0})))


def test_exit_approval_is_single_use_and_cannot_be_turned_into_a_buy() -> None:
    risk = engine()
    portfolio = Portfolio(10_000.0)
    broker = PaperBroker(risk, portfolio, CostConfig(fee_rate=0.0, slippage_bps=0.0))
    entry = risk.evaluate(buy(quantity=5.0), context())
    assert entry.approval is not None
    broker.execute(entry.approval, 100.0, NOW)
    exit_ = risk.evaluate(sell(5.0, reason=OrderReason.EMERGENCY), context(positions={SYMBOL: 5.0}))
    assert exit_.approval is not None
    fill = broker.execute(exit_.approval, 60.0, NOW)  # any price: the market decides
    assert fill.reason is OrderReason.EMERGENCY
    assert portfolio.positions == {}
    with pytest.raises(RiskViolationError, match="already used"):
        broker.execute(exit_.approval, 60.0, NOW)


# ------------------------------------------------------------- session: mechanics
def drive(
    config: AppConfig,
    closes: list[float],
    strategy: ScriptedStrategy,
    hooks: dict[int, Callable[[TradingSession], None]] | None = None,
    kill_switch: KillSwitch | None = None,
    restart_at: set[int] | None = None,
) -> TradingSession:
    """Feed candles bar by bar. ``hooks[i]`` runs after bar *i* closed (an operator
    acting between two candles); ``restart_at`` rebuilds the session from its
    snapshot after those bars (a process restart)."""
    cfg = config.model_copy(update={"costs": CostConfig(fee_rate=0.0, slippage_bps=0.0)})
    data = {TEST_SYMBOL: make_candles(closes, spread=0.0)}
    signals = compute_signal_frames(strategy, data)
    kill_switch = kill_switch or KillSwitch()
    session = build_session(cfg, strategy, TradingMode.PAPER, kill_switch)
    fills: list[Any] = []
    for i, (timestamp, bars, rows) in enumerate(iter_bars(data, signals, None, None)):
        session.process_bar(timestamp.to_pydatetime(), bars, rows)
        if hooks and i in hooks:
            hooks[i](session)
        if restart_at and i in restart_at:
            fills += session.portfolio.fills
            state = session.snapshot()
            session = build_session(cfg, strategy, TradingMode.PAPER, kill_switch)
            session.restore(state)
    session.all_fills = [*fills, *session.portfolio.fills]  # type: ignore[attr-defined]
    return session


def reasons(session: TradingSession) -> list[OrderReason]:
    return [f.reason for f in session.all_fills if f.side is Side.SELL]  # type: ignore[attr-defined]


def test_stop_loss_closes_the_position_even_with_the_kill_switch_active(
    config: AppConfig,
) -> None:
    switch = KillSwitch()
    session = drive(
        config,
        [100, 100, 100, 100, 70, 70],
        ScriptedStrategy({1}, set(), stop=20.0),
        hooks={2: lambda s: switch.activate("operator stop", source="operator")},
        kill_switch=switch,
    )
    assert reasons(session) == [OrderReason.STOP_LOSS]
    assert session.portfolio.positions == {}


def test_trailing_stop_is_reported_as_such(config: AppConfig) -> None:
    session = drive(
        config, [100, 100, 100, 130, 160, 150, 138, 138], ScriptedStrategy({1}, set(), 20.0, 20.0)
    )
    assert reasons(session) == [OrderReason.TRAILING_STOP]


def test_manual_close_executes_at_the_next_open(config: AppConfig) -> None:
    candles = [100, 100, 100, 110, 120, 125, 125]
    session = drive(
        config,
        candles,
        ScriptedStrategy({1}, set(), stop=50.0),
        hooks={3: lambda s: s.request_exit(TEST_SYMBOL)},
    )
    (exit_fill,) = [f for f in session.all_fills if f.side is Side.SELL]  # type: ignore[attr-defined]
    assert exit_fill.reason is OrderReason.MANUAL
    assert exit_fill.price == 110.0  # open of bar 4 = close of bar 3
    assert session.portfolio.positions == {}


def test_partial_close_keeps_the_rest_protected_by_the_stop(config: AppConfig) -> None:
    session = drive(
        config,
        [100, 100, 100, 120, 120, 120, 70, 70],
        ScriptedStrategy({1}, set(), stop=20.0),
        hooks={
            3: lambda s: s.request_exit(TEST_SYMBOL, OrderReason.TAKE_PROFIT, fraction=0.4),
        },
    )
    entry, partial, stop = session.all_fills  # type: ignore[attr-defined]
    assert partial.reason is OrderReason.TAKE_PROFIT
    assert partial.quantity == pytest.approx(entry.quantity * 0.4)
    assert stop.reason is OrderReason.STOP_LOSS
    assert stop.quantity == pytest.approx(entry.quantity * 0.6)
    assert stop.price == pytest.approx(80.0)  # the original stop, untouched by the partial
    assert session.portfolio.positions == {}
    assert [t.exit_reason for t in session.trades] == [
        OrderReason.TAKE_PROFIT,
        OrderReason.STOP_LOSS,
    ]
    assert session.trades[0].pnl == pytest.approx(entry.quantity * 0.4 * 20.0)


def test_emergency_flatten_on_kill_switch(config: AppConfig) -> None:
    flatten = config.model_copy(
        update={"risk": config.risk.model_copy(update={"flatten_on_kill_switch": True})}
    )
    switch = KillSwitch()
    session = drive(
        flatten,
        [100, 100, 100, 105, 106, 107, 108],
        ScriptedStrategy({1, 5}, set(), stop=50.0),
        hooks={3: lambda s: switch.activate("operator stop", source="operator")},
        kill_switch=switch,
    )
    assert reasons(session) == [OrderReason.KILL_SWITCH]
    assert session.portfolio.positions == {}  # and the later entry signal was vetoed
    rejected = [d for d in session.risk.journal.decisions if not d.approved]
    assert rejected
    assert all(d.order.side is Side.BUY for d in rejected)


def test_operator_emergency_close_of_everything(config: AppConfig) -> None:
    session = drive(
        config,
        [100, 100, 100, 105, 106, 107],
        ScriptedStrategy({1}, set(), stop=50.0),
        hooks={3: lambda s: s.request_exit(TEST_SYMBOL, OrderReason.EMERGENCY)},
    )
    assert reasons(session) == [OrderReason.EMERGENCY]


def test_exit_request_validation(config: AppConfig) -> None:
    session = drive(config, [100, 100, 100, 105], ScriptedStrategy({1}, set(), stop=50.0))
    with pytest.raises(ValueError, match="entry reason"):
        session.request_exit(TEST_SYMBOL, OrderReason.ENTRY)
    for fraction in (0.0, -0.5, 1.5, math.nan):
        with pytest.raises(ValueError, match="fraction"):
            session.request_exit(TEST_SYMBOL, fraction=fraction)
    assert session.request_exit("NOPE/USDT") is False
    assert session.request_exit(TEST_SYMBOL) is True


def test_restart_with_an_open_position_keeps_stop_pending_exit_and_trailing(
    config: AppConfig,
) -> None:
    """Same candles, with and without a process restart after every bar."""
    closes = [100, 100, 100, 130, 160, 150, 138, 138, 138]
    strategy = ScriptedStrategy({1}, set(), 20.0, 20.0)
    plain = drive(config, closes, strategy)
    restarted = drive(config, closes, strategy, restart_at=set(range(len(closes))))
    assert reasons(restarted) == reasons(plain) == [OrderReason.TRAILING_STOP]
    assert [f.price for f in restarted.all_fills] == [f.price for f in plain.all_fills]  # type: ignore[attr-defined]
    assert restarted.portfolio.cash == pytest.approx(plain.portfolio.cash)

    # a partial exit queued just before the restart is still executed after it
    partial = drive(
        config,
        [100, 100, 100, 120, 120, 120],
        ScriptedStrategy({1}, set(), stop=50.0),
        hooks={3: lambda s: s.request_exit(TEST_SYMBOL, OrderReason.MANUAL, 0.5)},
        restart_at={3},
    )
    assert reasons(partial) == [OrderReason.MANUAL]
    assert len(partial.portfolio.positions) == 1  # half of it is still open, stop intact
    assert partial.portfolio.positions[TEST_SYMBOL].stop_price == pytest.approx(50.0)


def test_exits_do_not_change_backtest_results(config: AppConfig) -> None:
    """The reduce-only path is a re-arrangement of checks, not of behaviour."""
    closes = [100.0] * 3 + [110, 120, 90, 80, 100, 130, 125]
    result = run_backtest(
        config.model_copy(update={"costs": CostConfig(fee_rate=0.0, slippage_bps=0.0)}),
        {TEST_SYMBOL: make_candles(closes, spread=0.0)},
        ScriptedStrategy({1, 6}, {8}, stop=15.0),
    )
    assert [f.reason for f in result.fills] == [
        OrderReason.ENTRY,
        OrderReason.STOP_LOSS,
        OrderReason.ENTRY,
        OrderReason.EXIT_SIGNAL,
    ]
    assert all(d.approved for d in result.decisions)
