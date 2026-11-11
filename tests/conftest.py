"""Shared fixtures. Market data here is *synthetic and seeded*: it exists to
exercise code paths deterministically, never to estimate performance."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from qts.core.config import AppConfig
from qts.core.logging import configure_logging


@pytest.fixture(autouse=True, scope="session")
def _quiet_logs() -> None:
    configure_logging("CRITICAL", json_output=True)


def make_candles(
    closes: list[float] | np.ndarray, start: str = "2021-01-01", spread: float = 0.01
) -> pd.DataFrame:
    """Build canonical daily candles from a close path (open = previous close)."""
    close = np.asarray(closes, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    index = pd.date_range(start, periods=len(close), freq="D", tz="UTC", name="timestamp")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1000.0}, index=index
    )


def random_walk(n: int, seed: int, drift: float = 0.0005, vol: float = 0.03) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    return make_candles(closes, start="2018-01-01")


@pytest.fixture
def config() -> AppConfig:
    """Default configuration restricted to one synthetic symbol."""
    return AppConfig.model_validate(
        {
            "market": {"symbols": ["TEST/USDT"]},
            "risk": {"allowed_symbols": ["TEST/USDT"]},
            "validation": {
                "walk_forward_train_days": 400,
                "walk_forward_test_days": 120,
                "monte_carlo_simulations": 200,
            },
        }
    )


@pytest.fixture
def walk_data() -> dict[str, pd.DataFrame]:
    return {"TEST/USDT": random_walk(1500, seed=7)}


@pytest.fixture
def now() -> datetime:
    return datetime(2024, 1, 10, tzinfo=UTC)
