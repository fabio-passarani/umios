from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qts.backtesting.validation import (
    common_index,
    evaluate_period,
    monte_carlo,
    run_validation,
    split_point,
    walk_forward,
)
from qts.core.config import AppConfig
from qts.core.errors import DataError
from tests.conftest import random_walk

SYMBOL = "TEST/USDT"


def test_split_is_chronological(walk_data: dict[str, pd.DataFrame]) -> None:
    index = common_index(walk_data)
    split = split_point(index, 0.3)
    assert split == index[1050]
    with pytest.raises(DataError):
        split_point(index[:5], 0.3)


def test_in_and_out_of_sample_do_not_overlap(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    report = run_validation(config, walk_data)
    assert report.in_sample.end < report.out_of_sample.start
    assert report.in_sample.start == report.full.start
    assert report.out_of_sample.end == report.full.end
    assert report.config_fingerprint == config.fingerprint()
    assert any("survivorship" in w for w in report.warnings)


def test_in_sample_result_is_independent_of_out_of_sample_data(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    """Data leakage guard: changing the OOS segment cannot alter IS results."""
    split = split_point(common_index(walk_data), 0.3)
    end = split - pd.Timedelta(days=1)
    original = evaluate_period(config, walk_data, "is", end=end)
    tampered_frame = walk_data[SYMBOL].copy()
    tampered_frame.loc[tampered_frame.index >= split] *= 3.0
    tampered = evaluate_period(config, {SYMBOL: tampered_frame}, "is", end=end)
    assert original.strategy == tampered.strategy
    assert original.benchmark == tampered.benchmark


def test_walk_forward_windows_are_sequential_and_out_of_sample(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    report = walk_forward(config, walk_data)
    assert len(report.windows) >= 5
    for window in report.windows:
        assert window.train_end < window.test_start  # parameters chosen strictly before
        assert set(window.chosen_params) == {"breakout_window", "stop_atr_multiple"}
    starts = [w.test_start for w in report.windows]
    assert starts == sorted(starts)
    assert report.oos_returns.index.is_unique
    assert report.oos_returns.index.is_monotonic_increasing
    assert report.oos_metrics["trades"] == sum(w.test_trades for w in report.windows)


def test_walk_forward_choice_does_not_depend_on_test_data(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    report = walk_forward(config, walk_data)
    first = report.windows[0]
    tampered = walk_data[SYMBOL].copy()
    tampered.loc[tampered.index >= first.test_start] *= 0.5
    again = walk_forward(config, {SYMBOL: tampered})
    assert again.windows[0].chosen_params == first.chosen_params
    assert again.windows[0].train_sharpe == pytest.approx(first.train_sharpe)


def test_walk_forward_needs_enough_history(config: AppConfig) -> None:
    short = {SYMBOL: random_walk(300, seed=1)}
    with pytest.raises(DataError, match="walk-forward"):
        walk_forward(config, short)


def test_walk_forward_rejects_unknown_grid_key(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    bad = config.model_copy(
        update={"validation": config.validation.model_copy(update={"parameter_grid": {"x": (1,)}})}
    )
    with pytest.raises(DataError, match="parameter_grid"):
        walk_forward(bad, walk_data)


def test_monte_carlo_is_reproducible_and_seed_sensitive() -> None:
    rng = np.random.default_rng(0)
    returns = pd.Series(rng.normal(0.0005, 0.01, 500))
    a = monte_carlo(returns, 300, seed=1)
    b = monte_carlo(returns, 300, seed=1)
    c = monte_carlo(returns, 300, seed=2)
    assert a == b
    assert a.total_return != c.total_return
    assert a.total_return["p05"] <= a.total_return["p50"] <= a.total_return["p95"]
    assert 0.0 <= a.probability_of_loss <= 1.0
    assert all(0.0 <= v <= 1.0 for v in a.max_drawdown.values())


def test_monte_carlo_on_always_positive_returns_never_loses() -> None:
    report = monte_carlo(pd.Series(np.full(200, 0.001)), 100, seed=3)
    assert report.probability_of_loss == 0.0
    assert report.max_drawdown["p95"] == 0.0
    assert report.total_return["p50"] == pytest.approx(1.001**200 - 1)
    assert report.observed["total_return"] == pytest.approx(1.001**200 - 1)


def test_monte_carlo_needs_enough_data() -> None:
    with pytest.raises(DataError):
        monte_carlo(pd.Series([0.01] * 10), 100, seed=1)
