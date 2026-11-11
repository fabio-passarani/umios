"""Causal feature engineering: every feature at row *t* uses data up to *t* only."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qts.strategies.indicators import atr, ema

FEATURE_COLUMNS: tuple[str, ...] = (
    "ret_1d",
    "ret_5d",
    "ret_20d",
    "ret_60d",
    "vol_20d",
    "vol_60d",
    "vol_ratio",
    "atr_pct",
    "ema_fast_slow",
    "close_to_ema_slow",
    "drawdown_90d",
    "range_position_60d",
    "volume_z_20d",
)


def build_features(candles: pd.DataFrame) -> pd.DataFrame:
    """Return the feature matrix aligned to ``candles`` (NaN during warm-up)."""
    close, high, low, volume = (candles[c] for c in ("close", "high", "low", "volume"))
    log_ret = np.log(close).diff()
    out = pd.DataFrame(index=candles.index)
    out["ret_1d"] = log_ret
    out["ret_5d"] = log_ret.rolling(5).sum()
    out["ret_20d"] = log_ret.rolling(20).sum()
    out["ret_60d"] = log_ret.rolling(60).sum()
    out["vol_20d"] = log_ret.rolling(20).std()
    out["vol_60d"] = log_ret.rolling(60).std()
    out["vol_ratio"] = out["vol_20d"] / out["vol_60d"]
    out["atr_pct"] = atr(high, low, close, 20) / close
    slow = ema(close, 200)
    out["ema_fast_slow"] = ema(close, 50) / slow - 1.0
    out["close_to_ema_slow"] = close / slow - 1.0
    out["drawdown_90d"] = close / close.rolling(90).max() - 1.0
    rolling_low, rolling_high = low.rolling(60).min(), high.rolling(60).max()
    out["range_position_60d"] = (close - rolling_low) / (rolling_high - rolling_low)
    volume_mean, volume_std = volume.rolling(20).mean(), volume.rolling(20).std()
    z = (volume - volume_mean) / volume_std.replace(0.0, np.nan)
    # Constant volume has no surprise: define the z-score as 0 instead of NaN.
    out["volume_z_20d"] = z.mask(volume_std == 0.0, 0.0)
    return out.loc[:, list(FEATURE_COLUMNS)]
