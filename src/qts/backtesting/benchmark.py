"""Buy & Hold benchmark.

The honest question for any active strategy is "did it beat simply holding?".
The benchmark buys the same universe equal-weighted at the first open, pays the
same fee and slippage once, and never trades again.
"""

from __future__ import annotations

import pandas as pd

from qts.core.config import CostConfig
from qts.core.errors import DataError


def buy_and_hold(
    data: dict[str, pd.DataFrame],
    initial_capital: float,
    costs: CostConfig,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> pd.Series:
    """Equity curve of an equal-weight buy-and-hold portfolio over ``[start, end]``.

    Each symbol's allocation is invested at that symbol's first available open
    in the window (cash until then), so late listings do not leak future data.
    """
    if not data:
        raise DataError("benchmark needs at least one symbol")
    allocation = initial_capital / len(data)
    curves: list[pd.Series] = []
    for symbol, frame in data.items():
        window = frame
        if start is not None:
            window = window.loc[window.index >= start]
        if end is not None:
            window = window.loc[window.index <= end]
        if window.empty:
            raise DataError(f"benchmark: no data for {symbol} in the window")
        entry_price = float(window["open"].iloc[0]) * (1.0 + costs.slippage_rate)
        quantity = allocation / (entry_price * (1.0 + costs.fee_rate))
        curves.append((window["close"] * quantity).rename(symbol))
    table = pd.concat(curves, axis=1).sort_index()
    # Before a symbol's first bar its allocation is still cash; after, hold last value.
    table = table.ffill().fillna(allocation)
    return table.sum(axis=1).rename("buy_and_hold")
