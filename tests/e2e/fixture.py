"""Deterministic synthetic market for the end-to-end test.

Two correlated assets going through the phases a trend follower reacts to:
a quiet base, a strong rally (entries), a crash (stop losses), a dull period
(no trend), a second rally (entries again — used to prove the kill switch
blocks them) and a slow decline (exit signals). The numbers are invented and
seeded: they exercise the whole chain, they say nothing about performance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

START = "2023-01-01"
WARMUP = 300  # candles of history before the paper session starts
SYMBOLS = ("BTC/USDT", "ETH/USDT")
#: (number of days, mean daily log-return, daily volatility)
PHASES = (
    (260, 0.0000, 0.012),  # base
    (85, 0.0090, 0.014),  # rally
    (1, -0.5000, 0.005),  # crash: one candle far below the protective stops
    (8, -0.0100, 0.020),  # aftermath
    (70, -0.0010, 0.012),  # dull
    (85, 0.0085, 0.014),  # second rally
    (70, -0.0060, 0.008),  # slow decline
)


def build(seed: int = 20261111) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    days = sum(p[0] for p in PHASES)
    drift = np.concatenate([np.full(n, mu) for n, mu, _ in PHASES])
    vol = np.concatenate([np.full(n, sigma) for n, _, sigma in PHASES])
    common = rng.normal(0.0, 1.0, days)
    index = pd.date_range(START, periods=days, freq="D", tz="UTC", name="timestamp")
    frames: dict[str, pd.DataFrame] = {}
    for symbol, base, beta in zip(SYMBOLS, (30_000.0, 2_000.0), (1.0, 1.15), strict=True):
        own = rng.normal(0.0, 1.0, days)
        returns = beta * drift + vol * (0.85 * common + 0.53 * own)
        close = base * np.exp(np.cumsum(returns))
        open_ = np.concatenate([[base], close[:-1]])
        wiggle = np.abs(rng.normal(0.0, 0.004, days))
        frames[symbol] = pd.DataFrame(
            {
                "open": open_,
                "high": np.maximum(open_, close) * (1.0 + wiggle),
                "low": np.minimum(open_, close) * (1.0 - wiggle),
                "close": close,
                "volume": 1_000.0 + 100.0 * np.abs(common),
            },
            index=index,
        )
    return frames
