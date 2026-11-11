"""The risk engine cannot be skipped, forged, replayed or out-voted."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import numpy as np
import pytest

import qts.risk.engine as risk_engine_module
from qts.backtesting.engine import run_backtest
from qts.core.config import AppConfig, CostConfig
from qts.core.errors import LiveTradingDisabledError, RiskViolationError
from qts.core.types import Side
from qts.execution import LiveBroker, OrderRouter, PaperBroker
from qts.portfolio import Portfolio
from qts.risk.models import RiskApproval
from qts.strategies import TrendFollowingStrategy
from tests.conftest import make_candles
from tests.risk.helpers import NOW, SYMBOL, buy, context, engine


def setup():  # type: ignore[no-untyped-def]
    risk = engine()
    portfolio = Portfolio(10_000.0)
    broker = PaperBroker(risk, portfolio, CostConfig(fee_rate=0.001, slippage_bps=5))
    return risk, portfolio, broker


def test_broker_refuses_a_raw_order() -> None:
    _risk, portfolio, broker = setup()
    with pytest.raises(RiskViolationError, match="no risk approval"):
        broker.execute(buy(), 100.0, NOW)  # type: ignore[arg-type]
    assert portfolio.cash == 10_000.0
    assert portfolio.positions == {}


def test_broker_refuses_a_forged_approval() -> None:
    _risk, portfolio, broker = setup()
    forged = RiskApproval(buy(), NOW, "f" * 64)
    with pytest.raises(RiskViolationError, match="signature"):
        broker.execute(forged, 100.0, NOW)
    assert portfolio.positions == {}


def test_broker_refuses_an_approval_altered_after_signing() -> None:
    risk, portfolio, broker = setup()
    decision = risk.evaluate(buy(quantity=1.0), context())
    assert decision.approval is not None
    bigger = dataclasses.replace(decision.approval.order, quantity=50.0)
    tampered = dataclasses.replace(decision.approval, order=bigger)
    with pytest.raises(RiskViolationError, match="signature"):
        broker.execute(tampered, 100.0, NOW)
    no_stop = dataclasses.replace(decision.approval.order, stop_price=None)
    with pytest.raises(RiskViolationError, match="signature"):
        broker.execute(dataclasses.replace(decision.approval, order=no_stop), 100.0, NOW)
    assert portfolio.positions == {}


def test_approval_is_single_use() -> None:
    risk, portfolio, broker = setup()
    decision = risk.evaluate(buy(quantity=1.0), context())
    assert decision.approval is not None
    broker.execute(decision.approval, 100.0, NOW)
    with pytest.raises(RiskViolationError, match="already used"):
        broker.execute(decision.approval, 100.0, NOW)
    assert portfolio.positions[SYMBOL].quantity == 1.0


def test_approval_from_another_engine_is_rejected() -> None:
    _risk, portfolio, broker = setup()
    other = engine()
    decision = other.evaluate(buy(quantity=1.0), context())
    assert decision.approval is not None
    with pytest.raises(RiskViolationError):
        broker.execute(decision.approval, 100.0, NOW)
    assert portfolio.positions == {}


def test_rejected_decision_carries_no_approval_and_router_does_not_execute() -> None:
    risk, portfolio, broker = setup()
    router = OrderRouter(risk, broker)
    decision, fill = router.submit(buy(quantity=1000.0), context(), 100.0, NOW)
    assert not decision.approved
    assert decision.approval is None
    assert fill is None
    assert portfolio.cash == 10_000.0


def test_router_executes_only_what_was_approved() -> None:
    risk, portfolio, broker = setup()
    decision, fill = OrderRouter(risk, broker).submit(buy(quantity=2.0), context(), 100.0, NOW)
    assert decision.approved
    assert fill is not None
    assert fill.side is Side.BUY
    assert fill.price == pytest.approx(100.05)  # 5 bps adverse slippage
    assert fill.fee == pytest.approx(2.0 * 100.05 * 0.001)
    assert portfolio.positions[SYMBOL].stop_price == 90.0


def test_risk_engine_has_no_override_switch() -> None:
    parameters = inspect.signature(risk_engine_module.RiskEngine.evaluate).parameters
    assert set(parameters) == {"self", "order", "context"}
    forbidden = ("force", "override", "bypass", "skip")
    source = inspect.getsource(risk_engine_module).lower()
    assert not any(f"{word}=" in source or f"{word}:" in source for word in forbidden)


def test_risk_engine_does_not_depend_on_strategies() -> None:
    """Independence is structural: the whole risk package imports no strategy/AI code."""
    package = Path(risk_engine_module.__file__).parent
    imported: set[str] = set()
    for file in package.glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
    offenders = {m for m in imported if m.startswith(("qts.strategies", "qts.ai", "qts.execution"))}
    assert offenders == set()


def test_live_broker_cannot_be_instantiated() -> None:
    with pytest.raises(LiveTradingDisabledError):
        LiveBroker()


def _trending_data() -> dict[str, object]:
    closes = np.concatenate([np.full(210, 100.0), 100.0 * 1.01 ** np.arange(1, 200)])
    return {"TEST/USDT": make_candles(closes, start="2020-01-01", spread=0.002)}


def test_strategy_signal_is_vetoed_when_risk_limit_is_tighter(config: AppConfig) -> None:
    """The strategy wants to trade, sizing proposes 1% risk, the engine allows 0.1%."""
    data = _trending_data()
    signals = TrendFollowingStrategy(config.strategy.params).generate_signals(data["TEST/USDT"])
    assert signals["entry"].any()  # the strategy does consider entries valid

    baseline = run_backtest(config, data)  # type: ignore[arg-type]
    assert baseline.fills  # sanity: with coherent limits it trades

    strict = config.model_copy(
        update={"risk": config.risk.model_copy(update={"max_risk_per_trade_pct": 0.001})}
    )
    vetoed = run_backtest(strict, data)  # type: ignore[arg-type]
    assert vetoed.fills == []
    assert vetoed.trades == []
    assert vetoed.equity.iloc[-1] == strict.portfolio.initial_capital
    rejected = [d for d in vetoed.decisions if not d.approved]
    assert rejected
    assert all("max_risk_per_trade" in d.reason for d in rejected)


def test_kill_switch_stops_a_running_backtest(config: AppConfig) -> None:
    """A drawdown limit breach latches the kill switch: no entries afterwards."""
    rng = np.random.default_rng(11)
    up = 100.0 * 1.02 ** np.arange(1, 120)
    chop = up[-1] * np.exp(np.cumsum(rng.normal(-0.004, 0.06, 600)))
    data = {
        "TEST/USDT": make_candles(
            np.concatenate([np.full(210, 100.0), up, chop]), start="2018-01-01", spread=0.03
        )
    }
    tight = config.model_copy(
        update={"risk": config.risk.model_copy(update={"max_drawdown_pct": 0.005})}
    )
    result = run_backtest(tight, data)
    assert result.kill_switch_active
    trip = next(e for e in result.risk_events if e.kind.startswith("kill_switch"))
    entries_after = [f for f in result.fills if f.side is Side.BUY and f.timestamp > trip.timestamp]
    assert entries_after == []
    blocked = [d for d in result.decisions if not d.approved and d.timestamp > trip.timestamp]
    assert blocked  # the strategy kept asking; the engine kept refusing
    assert all("kill_switch" in d.reason for d in blocked)
