from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qts.ai.features import FEATURE_COLUMNS, build_features
from qts.ai.regimes import build_dataset, label_regimes, purged_walk_forward_splits
from tests.conftest import make_candles, random_walk

AI_DIR = Path(__file__).resolve().parents[2] / "src" / "qts" / "ai"


def test_ai_package_cannot_reach_execution_or_risk() -> None:
    """The AI layer only produces analysis: it must not import order/risk code."""
    imported: set[str] = set()
    for file in AI_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
    forbidden = ("qts.execution", "qts.risk", "qts.integrations", "qts.portfolio", "ccxt")
    assert {m for m in imported if m.startswith(forbidden)} == set()


def test_features_are_causal() -> None:
    candles = random_walk(600, seed=4)
    full = build_features(candles)
    assert list(full.columns) == list(FEATURE_COLUMNS)
    partial = build_features(candles.iloc[:400])
    pd.testing.assert_frame_equal(partial, full.iloc[:400])


def test_labels_look_forward_and_leave_the_tail_unlabelled() -> None:
    closes = [100.0] * 5 + [120.0] * 5 + [90.0] * 5
    labels = label_regimes(make_candles(closes), horizon=5, threshold=0.05)
    assert labels.iloc[0] == 2  # +20% over the next 5 bars
    assert labels.iloc[5] == 0  # -25%
    assert labels.iloc[-5:].isna().all()
    features, y = build_dataset(random_walk(800, seed=4), horizon=20)
    assert len(features) == len(y) > 500
    assert features.notna().all().all()
    assert set(y.unique()) <= {0, 1, 2}


def test_purged_splits_never_overlap_and_keep_the_gap() -> None:
    splits = list(purged_walk_forward_splits(1000, n_splits=4, purge=20))
    assert len(splits) == 4
    for train, test in splits:
        assert test[0] - train[-1] > 20  # label horizon cannot straddle the split
        assert train[0] == 0
    assert splits[-1][1][-1] == 999
    with pytest.raises(ValueError, match="not enough"):
        list(purged_walk_forward_splits(300, n_splits=10, purge=20))


@pytest.mark.ai
def test_classifier_trains_evaluates_explains_and_tunes() -> None:
    pytest.importorskip("lightgbm")
    pytest.importorskip("shap")
    pytest.importorskip("optuna")
    from qts.ai.model import RegimeClassifier, evaluate_out_of_sample, tune_hyperparameters

    features, labels = build_dataset(random_walk(1500, seed=4), horizon=20)
    model = RegimeClassifier({"n_estimators": 30}).fit(features, labels)
    proba = model.predict_proba(features.iloc[-50:])
    assert list(proba.columns) == ["p_bear", "p_sideways", "p_bull"]
    assert np.allclose(proba.sum(axis=1), 1.0)
    again = RegimeClassifier({"n_estimators": 30}).fit(features, labels)
    pd.testing.assert_frame_equal(proba, again.predict_proba(features.iloc[-50:]))  # seeded

    report = evaluate_out_of_sample(
        features, labels, horizon=20, n_splits=3, params={"n_estimators": 30}
    )
    assert report.folds == 3
    assert 0.0 <= report.accuracy <= 1.0
    assert report.baseline_log_loss > 0
    assert isinstance(report.beats_baseline, bool)

    importance = model.explain(features.iloc[-200:])
    assert set(importance.index) == set(FEATURE_COLUMNS)
    assert (importance >= 0).all()
    assert importance.is_monotonic_decreasing

    params, score = tune_hyperparameters(features, labels, horizon=20, n_trials=3, n_splits=2)
    assert "num_leaves" in params
    assert score > 0
