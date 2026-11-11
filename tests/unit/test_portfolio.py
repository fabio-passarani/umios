from datetime import UTC, datetime

import pytest

from qts.core.config import SizingConfig
from qts.core.errors import ExecutionError
from qts.core.types import Fill, OrderReason, Side
from qts.portfolio import Portfolio, PositionSizer

T0 = datetime(2024, 1, 1, tzinfo=UTC)
T1 = datetime(2024, 1, 5, tzinfo=UTC)


def fill(side: Side, qty: float, price: float, fee: float = 0.0, at: datetime = T0) -> Fill:
    reason = OrderReason.ENTRY if side is Side.BUY else OrderReason.EXIT_SIGNAL
    return Fill("o", "BTC/USDT", side, qty, price, fee, at, reason)


def test_round_trip_accounting_with_fees() -> None:
    portfolio = Portfolio(1000.0)
    assert portfolio.apply_fill(fill(Side.BUY, 2.0, 100.0, fee=1.0), stop_price=90.0) is None
    assert portfolio.cash == pytest.approx(799.0)
    assert portfolio.positions["BTC/USDT"].stop_price == 90.0
    assert portfolio.equity({"BTC/USDT": 110.0}) == pytest.approx(1019.0)
    trade = portfolio.apply_fill(fill(Side.SELL, 2.0, 120.0, fee=2.0, at=T1))
    assert trade is not None
    assert trade.pnl == pytest.approx(40.0 - 3.0)
    assert trade.return_pct == pytest.approx(37.0 / 200.0)
    assert trade.fees == pytest.approx(3.0)
    assert portfolio.cash == pytest.approx(1037.0)
    assert portfolio.positions == {}
    assert portfolio.cash - portfolio.initial_capital == pytest.approx(trade.pnl)


def test_partial_sell_splits_entry_fees() -> None:
    portfolio = Portfolio(1000.0)
    portfolio.apply_fill(fill(Side.BUY, 4.0, 100.0, fee=4.0))
    trade = portfolio.apply_fill(fill(Side.SELL, 1.0, 110.0, at=T1))
    assert trade is not None
    assert trade.fees == pytest.approx(1.0)
    assert portfolio.positions["BTC/USDT"].quantity == pytest.approx(3.0)
    assert portfolio.positions["BTC/USDT"].entry_fees == pytest.approx(3.0)


def test_adding_to_a_position_averages_price() -> None:
    portfolio = Portfolio(1000.0)
    portfolio.apply_fill(fill(Side.BUY, 1.0, 100.0))
    portfolio.apply_fill(fill(Side.BUY, 1.0, 200.0))
    assert portfolio.positions["BTC/USDT"].avg_price == pytest.approx(150.0)


def test_cannot_overspend_or_short() -> None:
    portfolio = Portfolio(100.0)
    with pytest.raises(ExecutionError, match="insufficient cash"):
        portfolio.apply_fill(fill(Side.BUY, 2.0, 100.0))
    with pytest.raises(ExecutionError, match="no shorts"):
        portfolio.apply_fill(fill(Side.SELL, 1.0, 100.0))
    portfolio.apply_fill(fill(Side.BUY, 0.5, 100.0))
    with pytest.raises(ExecutionError, match="no shorts"):
        portfolio.apply_fill(fill(Side.SELL, 0.6, 100.0))
    with pytest.raises(ExecutionError, match="invalid fill"):
        portfolio.apply_fill(fill(Side.BUY, -1.0, 100.0))
    assert portfolio.cash == pytest.approx(50.0)


def test_sizer_risks_fixed_fraction_of_equity() -> None:
    sizer = PositionSizer(SizingConfig(risk_per_trade_pct=0.01, max_position_pct=1.0))
    quantity = sizer.size(equity=10_000, cash=10_000, price=100.0, stop_distance=10.0)
    assert quantity == pytest.approx(10.0)  # 10 units x 10 stop distance = 100 = 1% of equity


def test_sizer_caps_by_position_limit_and_cash() -> None:
    sizer = PositionSizer(SizingConfig(risk_per_trade_pct=0.01, max_position_pct=0.3))
    capped = sizer.size(equity=10_000, cash=10_000, price=100.0, stop_distance=1.0)
    assert capped == pytest.approx(30.0)
    by_cash = sizer.size(equity=10_000, cash=1_000, price=100.0, stop_distance=1.0, cost_rate=0.01)
    assert by_cash == pytest.approx(1000 / 101.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"equity": 0, "cash": 1, "price": 1, "stop_distance": 1},
        {"equity": 1, "cash": 0, "price": 1, "stop_distance": 1},
        {"equity": 1, "cash": 1, "price": 1, "stop_distance": 0},
        {"equity": 1, "cash": 1, "price": float("nan"), "stop_distance": 1},
        {"equity": 1, "cash": 1, "price": 1, "stop_distance": float("inf")},
    ],
)
def test_sizer_returns_zero_on_invalid_inputs(kwargs: dict[str, float]) -> None:
    assert PositionSizer(SizingConfig()).size(**kwargs) == 0.0
