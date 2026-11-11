"""Canonical OHLCV schema and quality checks.

Every data source must return frames in this exact shape, so nothing downstream
(strategies, backtests, storage) ever depends on a vendor format:

* index: ``timestamp`` — tz-aware UTC ``DatetimeIndex`` marking the candle *open*;
* columns: ``open, high, low, close, volume`` as ``float64``;
* strictly increasing, no duplicates, only *closed* candles.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qts.core.errors import DataError

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
TIMEFRAME_TO_FREQ: dict[str, pd.Timedelta] = {"1d": pd.Timedelta(days=1)}


def normalize_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce a raw frame into the canonical schema (sorted, de-duplicated, UTC).

    Accepts either a ``timestamp`` column or a datetime index. Duplicate
    timestamps keep the last occurrence (a later download supersedes an earlier one).
    """
    df = frame.copy()
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise DataError("OHLCV data needs a datetime index or a 'timestamp' column")
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"OHLCV data is missing columns: {missing}")
    index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    df.index = pd.DatetimeIndex(index.as_unit("ns"), name="timestamp")
    df = df.loc[:, list(OHLCV_COLUMNS)].astype("float64")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


@dataclass(frozen=True)
class QualityReport:
    """Outcome of :func:`check_quality`. ``errors`` make the data unusable;
    ``warnings`` (e.g. gaps) are reported but do not block."""

    rows: int
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    missing_candles: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_quality(df: pd.DataFrame, timeframe: str = "1d") -> QualityReport:
    """Validate a canonical OHLCV frame.

    Gaps are *reported, never filled*: forward-filling prices would fabricate
    candles that never traded and distort indicators.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if df.empty:
        return QualityReport(0, None, None, 0, ["no rows"], [])
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        errors.append("timestamps are not strictly increasing")
    values = df.loc[:, list(OHLCV_COLUMNS)]
    if not np.isfinite(values.to_numpy()).all():
        errors.append("NaN or infinite values present")
    if (values[["open", "high", "low", "close"]] <= 0).any().any():
        errors.append("non-positive prices present")
    if (values["volume"] < 0).any():
        errors.append("negative volume present")
    bad_high = values["high"] < values[["open", "close", "low"]].max(axis=1)
    bad_low = values["low"] > values[["open", "close", "high"]].min(axis=1)
    if bool(bad_high.any() or bad_low.any()):
        errors.append(f"{int((bad_high | bad_low).sum())} candles violate low<=open,close<=high")
    step = TIMEFRAME_TO_FREQ[timeframe]
    expected = int((df.index[-1] - df.index[0]) / step) + 1
    missing = max(expected - len(df), 0)
    if missing:
        warnings.append(f"{missing} missing candles between first and last timestamp")
    index = pd.DatetimeIndex(df.index)
    if timeframe == "1d" and bool((index != index.normalize()).any()):
        errors.append("daily candles must be aligned to 00:00 UTC")
    return QualityReport(len(df), df.index[0], df.index[-1], missing, errors, warnings)


def require_quality(df: pd.DataFrame, symbol: str, timeframe: str = "1d") -> QualityReport:
    """Like :func:`check_quality` but raises :class:`DataError` on errors."""
    report = check_quality(df, timeframe)
    if not report.ok:
        raise DataError(f"{symbol}: data failed quality checks: {'; '.join(report.errors)}")
    return report
