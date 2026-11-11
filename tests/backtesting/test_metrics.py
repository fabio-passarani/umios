import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from qts.backtesting.metrics import (
    cagr,
    compute_metrics,
    drawdown_series,
    max_drawdown,
    max_drawdown_duration,
    sharpe_ratio,
    sortino_ratio,
    trade_statistics,
)
from qts.core.types import OrderReason, Trade

T = datetime(2024, 1, 1, tzinfo=UTC)


def trade(pnl: float, ret: float = 0.0, fees: float = 1.0) -> Trade:
    return Trade("X", 1.0, T, T, 100.0, 100.0 + pnl, fees, pnl, ret, OrderReason.EXIT_SIGNAL)


def test_max_drawdown_and_duration() -> None:
    equity = pd.Series([100.0, 120.0, 90.0, 96.0, 130.0, 117.0])
    assert max_drawdown(equity) == pytest.approx(0.25)  # 120 -> 90
    assert drawdown_series(equity).iloc[2] == pytest.approx(-0.25)
    assert max_drawdown_duration(equity) == 2
    assert max_drawdown(pd.Series([1.0, 2.0, 3.0])) == 0.0


def test_sharpe_and_sortino_hand_computed() -> None:
    returns = pd.Series([0.01, -0.02, 0.03, 0.00])
    mean = 0.005
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / 3)
    assert sharpe_ratio(returns) == pytest.approx(mean / std * math.sqrt(365))
    downside = math.sqrt((0.02**2) / 4)
    assert sortino_ratio(returns) == pytest.approx(mean / downside * math.sqrt(365))


def test_ratios_are_zero_when_undefined() -> None:
    flat = pd.Series([0.0, 0.0, 0.0])
    assert sharpe_ratio(flat) == 0.0
    assert sortino_ratio(pd.Series([0.01, 0.02])) == 0.0  # no downside
    assert sharpe_ratio(pd.Series([0.01])) == 0.0


def test_cagr_doubles_in_one_year() -> None:
    equity = pd.Series(np.linspace(100, 200, 365))
    assert cagr(equity, 100.0) == pytest.approx(1.0)


def test_trade_statistics() -> None:
    stats = trade_statistics(
        [trade(100, 0.10), trade(-50, -0.05), trade(50, 0.05), trade(-50, -0.05)]
    )
    assert stats["trades"] == 4
    assert stats["win_rate"] == 0.5
    assert stats["profit_factor"] == pytest.approx(150 / 100)
    assert stats["expectancy"] == pytest.approx(12.5)
    assert stats["expectancy_pct"] == pytest.approx(0.0125)
    assert stats["avg_win"] == 75.0
    assert stats["avg_loss"] == -50.0
    assert stats["total_fees"] == 4.0


def test_trade_statistics_edge_cases() -> None:
    assert trade_statistics([])["trades"] == 0
    assert trade_statistics([trade(10)])["profit_factor"] == math.inf
    assert trade_statistics([trade(-10)])["profit_factor"] == 0.0


def test_compute_metrics_counts_first_bar_loss_in_drawdown() -> None:
    index = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    metrics = compute_metrics(pd.Series([90.0, 95.0, 99.0], index=index), 100.0)
    assert metrics["total_return"] == pytest.approx(-0.01)
    assert metrics["max_drawdown"] == pytest.approx(0.10)  # measured from initial capital
    assert metrics["final_equity"] == 99.0
