from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from qts.core.config import RiskLimits
from qts.core.types import Order, OrderReason, Side, TradingMode
from qts.risk import KillSwitch, RiskContext, RiskEngine

NOW = datetime(2024, 1, 10, tzinfo=UTC)
SYMBOL = "BTC/USDT"


def limits(**overrides: Any) -> RiskLimits:
    base: dict[str, Any] = {
        "max_risk_per_trade_pct": 0.02,
        "max_position_pct": 0.5,
        "max_total_exposure_pct": 1.0,
        "max_open_positions": 2,
        "max_daily_loss_pct": 0.05,
        "max_drawdown_pct": 0.20,
        "max_orders_per_day": 3,
        "allowed_symbols": (SYMBOL, "ETH/USDT", "SOL/USDT"),
    }
    base.update(overrides)
    return RiskLimits(**base)


def engine(
    mode: TradingMode = TradingMode.PAPER, kill_switch: KillSwitch | None = None, **overrides: Any
) -> RiskEngine:
    return RiskEngine(limits(**overrides), mode, kill_switch=kill_switch)


def context(**overrides: Any) -> RiskContext:
    base: dict[str, Any] = {
        "now": NOW,
        "cash": 10_000.0,
        "positions": {},
        "prices": {SYMBOL: 100.0, "ETH/USDT": 10.0, "SOL/USDT": 1.0},
        "data_time": NOW,
        "cost_rate": 0.002,
    }
    base.update(overrides)
    return RiskContext(**base)


def buy(
    quantity: float = 10.0,
    price: float = 100.0,
    stop: float | None = 90.0,
    symbol: str = SYMBOL,
    **overrides: Any,
) -> Order:
    base: dict[str, Any] = {
        "symbol": symbol,
        "side": Side.BUY,
        "quantity": quantity,
        "reference_price": price,
        "reason": OrderReason.ENTRY,
        "strategy": "test",
        "created_at": NOW,
        "stop_price": stop,
    }
    base.update(overrides)
    return Order(**base)


def sell(
    quantity: float,
    price: float = 100.0,
    symbol: str = SYMBOL,
    reason: OrderReason = OrderReason.EXIT_SIGNAL,
) -> Order:
    return Order(symbol, Side.SELL, quantity, price, reason, "test", NOW)
