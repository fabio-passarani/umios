"""Volatility-based position sizing (fixed fractional risk)."""

from __future__ import annotations

import math

from qts.core.config import SizingConfig


class PositionSizer:
    """Sizes a position so that hitting the stop loses a fixed fraction of equity.

    ``quantity = equity * risk_per_trade_pct / stop_distance`` — volatile assets
    (wide ATR stop) automatically get smaller positions. The result is then
    capped by ``max_position_pct`` of equity and by the cash actually available
    (net of estimated costs). Sizing is a *proposal*: the risk engine re-checks
    every order against its own, independent limits.
    """

    def __init__(self, config: SizingConfig) -> None:
        self.config = config

    def size(
        self,
        equity: float,
        cash: float,
        price: float,
        stop_distance: float,
        cost_rate: float = 0.0,
    ) -> float:
        """Return the quantity to buy (0.0 when no valid size exists)."""
        inputs = (equity, cash, price, stop_distance)
        if not all(math.isfinite(v) for v in inputs) or min(inputs) <= 0:
            return 0.0
        by_risk = equity * self.config.risk_per_trade_pct / stop_distance
        by_position_cap = equity * self.config.max_position_pct / price
        by_cash = cash / (price * (1.0 + cost_rate))
        quantity = min(by_risk, by_position_cap, by_cash)
        return max(quantity, 0.0)
