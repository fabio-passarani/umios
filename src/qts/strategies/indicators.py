"""Technical indicators implemented in plain pandas.

They are written here (≈40 lines) rather than pulled from TA-Lib so the core has
no native dependency and — more importantly — so causality is explicit: every
function uses only data up to and including the current row.
"""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, window: int) -> pd.Series:
    """Exponential moving average; NaN until ``window`` observations exist."""
    return series.ewm(span=window, adjust=False, min_periods=window).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range: the largest of high-low and the gaps from the previous close."""
    prev_close = close.shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    return ranges.max(axis=1, skipna=True)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> pd.Series:
    """Average true range (Wilder smoothing)."""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def donchian_high(high: pd.Series, window: int) -> pd.Series:
    """Highest high of the *previous* ``window`` bars (current bar excluded)."""
    return high.rolling(window, min_periods=window).max().shift(1)


def donchian_low(low: pd.Series, window: int) -> pd.Series:
    """Lowest low of the *previous* ``window`` bars (current bar excluded)."""
    return low.rolling(window, min_periods=window).min().shift(1)
