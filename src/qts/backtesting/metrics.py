"""Performance statistics.

Implemented directly (and unit-tested against hand-computed values) so the
numbers the platform reports never depend on the conventions of a third-party
library changing underneath us. Crypto trades every day: 365 daily periods per
year; intraday timeframes pass their own count (``periods_per_year``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

from qts.core.types import Trade

PERIODS_PER_YEAR = 365


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Drawdown from the running peak, as non-positive fractions."""
    return (equity / equity.cummax() - 1.0).rename("drawdown")


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough loss as a positive fraction."""
    return float(-drawdown_series(equity).min()) if len(equity) else 0.0


def max_drawdown_duration(equity: pd.Series) -> int:
    """Longest stretch (in bars) spent below a previous equity peak."""
    underwater = (equity < equity.cummax()).to_numpy()
    longest = current = 0
    for flag in underwater:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def sharpe_ratio(returns: pd.Series, periods: int = PERIODS_PER_YEAR) -> float:
    """Annualised Sharpe ratio with a zero risk-free rate."""
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    if std == 0.0 or math.isnan(std):
        return 0.0
    return float(returns.mean()) / std * math.sqrt(periods)


def sortino_ratio(returns: pd.Series, periods: int = PERIODS_PER_YEAR) -> float:
    """Annualised Sortino ratio (downside deviation against a 0 target)."""
    if len(returns) < 2:
        return 0.0
    downside = math.sqrt(float((np.minimum(returns.to_numpy(), 0.0) ** 2).mean()))
    if downside == 0.0:
        return 0.0
    return float(returns.mean()) / downside * math.sqrt(periods)


def cagr(equity: pd.Series, initial_capital: float, periods: int = PERIODS_PER_YEAR) -> float:
    """Compound annual growth rate over the length of the curve."""
    if len(equity) == 0 or initial_capital <= 0 or equity.iloc[-1] <= 0:
        return 0.0
    years = len(equity) / periods
    return float((equity.iloc[-1] / initial_capital) ** (1.0 / years) - 1.0) if years > 0 else 0.0


def trade_statistics(trades: Sequence[Trade]) -> dict[str, float]:
    """Win rate, profit factor, expectancy and friends from closed trades."""
    count = len(trades)
    if count == 0:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "expectancy_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "total_fees": 0.0,
        }
    pnl = np.array([t.pnl for t in trades], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0
    return {
        "trades": count,
        "win_rate": float(len(wins) / count),
        "profit_factor": float(profit_factor),
        "expectancy": float(pnl.mean()),
        "expectancy_pct": float(np.mean([t.return_pct for t in trades])),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "total_fees": float(sum(t.fees for t in trades)),
    }


def compute_metrics(
    equity: pd.Series,
    initial_capital: float,
    trades: Sequence[Trade] = (),
    exposure: pd.Series | None = None,
    periods: int = PERIODS_PER_YEAR,
) -> dict[str, float]:
    """Full metric set for an equity curve and (optionally) its trades.

    ``periods`` is the number of candles per year of the equity curve's
    timeframe (365 for daily candles): it drives every annualised figure.
    """
    previous = equity.shift(1)
    if len(previous):
        previous.iloc[0] = initial_capital
    returns = equity / previous - 1.0
    growth = cagr(equity, initial_capital, periods)
    mdd = max_drawdown(pd.concat([pd.Series([initial_capital]), equity], ignore_index=True))
    metrics: dict[str, float] = {
        "initial_capital": float(initial_capital),
        "final_equity": float(equity.iloc[-1]) if len(equity) else float(initial_capital),
        "total_return": float(equity.iloc[-1] / initial_capital - 1.0) if len(equity) else 0.0,
        "cagr": growth,
        "annual_volatility": float(returns.std(ddof=1) * math.sqrt(periods))
        if len(returns) > 1
        else 0.0,
        "sharpe": sharpe_ratio(returns, periods),
        "sortino": sortino_ratio(returns, periods),
        "max_drawdown": mdd,
        "max_drawdown_duration_bars": float(max_drawdown_duration(equity)),
        "calmar": growth / mdd if mdd > 0 else 0.0,
        "bars": float(len(equity)),
    }
    if exposure is not None and len(exposure):
        metrics["time_in_market"] = float((exposure > 0).mean())
    metrics.update(trade_statistics(trades))
    return metrics
