"""Daily trend-following strategy (Donchian breakout with trend and volatility filters).

Logic, long-only:

* **Trend filter** — fast EMA above slow EMA: only trade in the direction of the
  established trend.
* **Entry** — close above the highest high of the previous ``breakout_window``
  bars, with volatility (ATR/close) inside ``[min_atr_pct, max_atr_pct]``.
* **Exit** — close below the lowest low of the previous ``exit_window`` bars, or
  the trend filter turning off.
* **Stop** — ``stop_atr_multiple`` ATRs below the entry fill; optionally trailed
  (chandelier) by ``trailing_atr_multiple`` ATRs from the highest close.

These are the textbook rules of the family (Donchian/"Turtle" style). Defaults
are conventional round values chosen *a priori*, not fitted to the data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from qts.core.config import TrendFollowingParams
from qts.strategies.base import Strategy
from qts.strategies.indicators import atr, donchian_high, donchian_low, ema


class TrendFollowingStrategy(Strategy):
    """See module docstring. All behaviour is driven by :class:`TrendFollowingParams`."""

    name = "trend_following"
    version = "1.0.0"

    def __init__(self, params: TrendFollowingParams | None = None) -> None:
        self.params = params or TrendFollowingParams()

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p.slow_ema, p.breakout_window + 1, p.exit_window + 1, p.atr_window + 1)

    def generate_signals(self, candles: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close, high, low = candles["close"], candles["high"], candles["low"]
        out = pd.DataFrame(index=candles.index)
        out["ema_fast"] = ema(close, p.fast_ema)
        out["ema_slow"] = ema(close, p.slow_ema)
        out["atr"] = atr(high, low, close, p.atr_window)
        out["atr_pct"] = out["atr"] / close
        out["breakout_high"] = donchian_high(high, p.breakout_window)
        out["exit_low"] = donchian_low(low, p.exit_window)

        uptrend = out["ema_fast"] > out["ema_slow"]
        volatility_ok = (out["atr_pct"] >= p.min_atr_pct) & (out["atr_pct"] <= p.max_atr_pct)
        ready = out[["ema_slow", "atr", "breakout_high", "exit_low"]].notna().all(axis=1)

        out["entry"] = (ready & uptrend & volatility_ok & (close > out["breakout_high"])).astype(
            bool
        )
        out["exit"] = (ready & ((close < out["exit_low"]) | ~uptrend)).astype(bool)
        out["stop_distance"] = out["atr"] * p.stop_atr_multiple
        out["trail_distance"] = out["atr"] * p.trailing_atr_multiple if p.trailing_stop else np.nan
        return out
