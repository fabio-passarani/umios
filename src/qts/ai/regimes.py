"""Market-regime labels and leakage-safe dataset splitting.

Labels look **forward** by design (they describe what happened next) and are
therefore usable only as training targets — never as features. Because a label
at *t* spans ``horizon`` future bars, train and test sets must be separated by
at least ``horizon`` bars ("purging"), otherwise information leaks across the
split and out-of-sample scores are inflated.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd

from qts.ai.features import build_features

REGIMES: tuple[str, ...] = ("bear", "sideways", "bull")


def label_regimes(candles: pd.DataFrame, horizon: int = 20, threshold: float = 0.05) -> pd.Series:
    """Label each bar by the *following* ``horizon``-bar return.

    0 = bear (< -threshold), 1 = sideways, 2 = bull (> +threshold). The last
    ``horizon`` bars have no label (NaN): their future is unknown.
    """
    close = candles["close"]
    forward = close.shift(-horizon) / close - 1.0
    labels = pd.Series(1.0, index=candles.index, name="regime")
    labels[forward > threshold] = 2.0
    labels[forward < -threshold] = 0.0
    labels[forward.isna()] = np.nan
    return labels


def build_dataset(
    candles: pd.DataFrame, horizon: int = 20, threshold: float = 0.05
) -> tuple[pd.DataFrame, pd.Series]:
    """Features and labels with incomplete rows (warm-up, unknown future) removed."""
    features = build_features(candles)
    labels = label_regimes(candles, horizon, threshold)
    valid = features.notna().all(axis=1) & labels.notna()
    return features.loc[valid], labels.loc[valid].astype(int)


def purged_walk_forward_splits(
    n_samples: int, n_splits: int, purge: int, min_train: int = 250
) -> Iterator[tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]]:
    """Expanding-window splits with a ``purge``-bar gap between train and test."""
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")
    test_size = (n_samples - min_train - purge) // n_splits
    if test_size < 10:
        raise ValueError("not enough samples for the requested splits")
    for i in range(n_splits):
        train_end = min_train + i * test_size
        test_start = train_end + purge
        test_end = n_samples if i == n_splits - 1 else test_start + test_size
        yield np.arange(0, train_end), np.arange(test_start, test_end)
