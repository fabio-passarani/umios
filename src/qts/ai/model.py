"""LightGBM regime classifier with out-of-sample evaluation, Optuna tuning and SHAP.

LightGBM is used because gradient-boosted trees are the strongest general
baseline on small tabular datasets, train in seconds on CPU and are directly
explainable with SHAP's exact tree algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from qts.ai.regimes import REGIMES, purged_walk_forward_splits

DEFAULT_PARAMS: dict[str, Any] = {
    "objective": "multiclass",
    "num_class": len(REGIMES),
    "n_estimators": 200,
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_child_samples": 40,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "verbosity": -1,
    "n_jobs": 1,
}


def _lightgbm() -> Any:
    try:
        import lightgbm
    except ImportError as exc:  # pragma: no cover
        raise ImportError("install the AI extra: pip install '.[ai]'") from exc
    return lightgbm


@dataclass
class EvaluationReport:
    """Out-of-sample scores next to the trivial baseline they must beat."""

    folds: int
    samples: int
    accuracy: float
    balanced_accuracy: float
    log_loss: float
    baseline_accuracy: float
    baseline_log_loss: float
    per_fold_accuracy: list[float] = field(default_factory=list)

    @property
    def beats_baseline(self) -> bool:
        return self.log_loss < self.baseline_log_loss and self.accuracy > self.baseline_accuracy


class RegimeClassifier:
    """Thin, reproducible wrapper around ``lightgbm.LGBMClassifier``."""

    def __init__(self, params: dict[str, Any] | None = None, seed: int = 42) -> None:
        self.params = {**DEFAULT_PARAMS, **(params or {}), "random_state": seed}
        self.seed = seed
        self.model: Any = None
        self.feature_names: list[str] = []

    def fit(self, features: pd.DataFrame, labels: pd.Series) -> RegimeClassifier:
        self.model = _lightgbm().LGBMClassifier(**self.params)
        self.model.fit(features, labels)
        self.feature_names = list(features.columns)
        return self

    def predict_proba(self, features: pd.DataFrame) -> pd.DataFrame:
        """Class probabilities as a frame with one column per regime."""
        if self.model is None:
            raise RuntimeError("model is not fitted")
        raw = self.model.predict_proba(features[self.feature_names])
        proba = np.zeros((len(features), len(REGIMES)))
        proba[:, [int(c) for c in self.model.classes_]] = raw
        return pd.DataFrame(proba, index=features.index, columns=[f"p_{r}" for r in REGIMES])

    def explain(self, features: pd.DataFrame) -> pd.Series:
        """Mean absolute SHAP value per feature (global importance), descending."""
        try:
            import shap
        except ImportError as exc:  # pragma: no cover
            raise ImportError("install the AI extra: pip install '.[ai]'") from exc
        if self.model is None:
            raise RuntimeError("model is not fitted")
        values = np.asarray(
            shap.TreeExplainer(self.model).shap_values(features[self.feature_names])
        )
        # Layout differs across shap versions: put the feature axis first, average the rest.
        feature_axis = (
            next(
                i
                for i, size in enumerate(values.shape)
                if size == len(self.feature_names) and i != 0
            )
            if values.ndim == 3
            else 1
        )
        importance = np.abs(np.moveaxis(values, feature_axis, 0)).reshape(
            len(self.feature_names), -1
        )
        return pd.Series(importance.mean(axis=1), index=self.feature_names).sort_values(
            ascending=False
        )


def _log_loss(labels: np.ndarray[Any, Any], proba: np.ndarray[Any, Any]) -> float:
    clipped = np.clip(proba[np.arange(len(labels)), labels], 1e-12, 1.0)
    return float(-np.log(clipped).mean())


def evaluate_out_of_sample(
    features: pd.DataFrame,
    labels: pd.Series,
    horizon: int,
    n_splits: int = 4,
    params: dict[str, Any] | None = None,
    seed: int = 42,
) -> EvaluationReport:
    """Purged walk-forward evaluation against a 'train class frequencies' baseline."""
    y = labels.to_numpy()
    predictions: list[np.ndarray[Any, Any]] = []
    baselines: list[np.ndarray[Any, Any]] = []
    truth: list[np.ndarray[Any, Any]] = []
    fold_accuracy: list[float] = []
    for train_idx, test_idx in purged_walk_forward_splits(len(features), n_splits, purge=horizon):
        model = RegimeClassifier(params, seed).fit(features.iloc[train_idx], labels.iloc[train_idx])
        proba = model.predict_proba(features.iloc[test_idx]).to_numpy()
        frequencies = np.bincount(y[train_idx], minlength=len(REGIMES)) / len(train_idx)
        predictions.append(proba)
        baselines.append(np.tile(frequencies, (len(test_idx), 1)))
        truth.append(y[test_idx])
        fold_accuracy.append(float((proba.argmax(axis=1) == y[test_idx]).mean()))
    p, b, t = np.vstack(predictions), np.vstack(baselines), np.concatenate(truth)
    predicted = p.argmax(axis=1)
    recalls = [float((predicted[t == c] == c).mean()) for c in np.unique(t)]
    return EvaluationReport(
        folds=n_splits,
        samples=len(t),
        accuracy=float((predicted == t).mean()),
        balanced_accuracy=float(np.mean(recalls)),
        log_loss=_log_loss(t, p),
        baseline_accuracy=float((b.argmax(axis=1) == t).mean()),
        baseline_log_loss=_log_loss(t, b),
        per_fold_accuracy=fold_accuracy,
    )


def tune_hyperparameters(
    features: pd.DataFrame,
    labels: pd.Series,
    horizon: int,
    n_trials: int = 30,
    n_splits: int = 3,
    seed: int = 42,
) -> tuple[dict[str, Any], float]:
    """Optuna search minimising purged walk-forward log loss. Returns (params, score).

    Tune on a development period only and confirm on data the search never saw:
    the returned score is optimistically biased by construction.
    """
    try:
        import optuna
    except ImportError as exc:  # pragma: no cover
        raise ImportError("install the AI extra: pip install '.[ai]'") from exc
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial: Any) -> float:
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 50, 400),
            "learning_rate": trial.suggest_float("learning_rate", 0.005, 0.2, log=True),
            "num_leaves": trial.suggest_int("num_leaves", 4, 31),
            "min_child_samples": trial.suggest_int("min_child_samples", 20, 120),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-2, 10.0, log=True),
        }
        return evaluate_out_of_sample(features, labels, horizon, n_splits, params, seed).log_loss

    study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=seed))
    study.optimize(objective, n_trials=n_trials)
    return dict(study.best_params), float(study.best_value)
