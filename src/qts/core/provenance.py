"""What a result was computed from: data, code, configuration.

A backtest number is only worth something if it can be produced again. Every
stored run therefore carries a *provenance* record with the exact period, the
universe, the strategy and its parameters, the costs, a content hash of the
candles that were used, the Git commit of the code and a hash of the resulting
equity curve. ``qts reproduce`` re-runs a stored run and compares all of it.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qts import __version__

_COLUMNS = ("open", "high", "low", "close", "volume")


def _frame_digest(frame: pd.DataFrame) -> str:
    """SHA-256 of a candle frame's content (timestamps in ns + OHLCV as float64).

    Depends only on the values, not on file format, compression or library
    version, so the same candles always give the same digest.
    """
    digest = hashlib.sha256()
    index = pd.DatetimeIndex(frame.index).tz_convert("UTC").as_unit("ns")
    digest.update(np.ascontiguousarray(index.asi8, dtype="<i8").tobytes())
    for column in _COLUMNS:
        digest.update(np.ascontiguousarray(frame[column].to_numpy(dtype="<f8")).tobytes())
    return digest.hexdigest()


def data_version(data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Identify the dataset: one digest per symbol and one for the whole universe."""
    symbols: dict[str, Any] = {}
    combined = hashlib.sha256()
    for symbol in sorted(data):
        frame = data[symbol]
        digest = _frame_digest(frame)
        combined.update(f"{symbol}:{digest};".encode())
        symbols[symbol] = {
            "sha256": digest,
            "rows": len(frame),
            "first": frame.index[0].isoformat() if len(frame) else None,
            "last": frame.index[-1].isoformat() if len(frame) else None,
        }
    return {"id": combined.hexdigest()[:16], "symbols": symbols}


def result_hash(equity: pd.Series) -> str:
    """Digest of an equity curve, rounded to 1e-6 so it is stable across CPUs
    while still catching any real difference."""
    values = np.round(equity.to_numpy(dtype="<f8"), 6)
    index = pd.DatetimeIndex(equity.index).tz_convert("UTC").as_unit("ns").asi8
    digest = hashlib.sha256(np.ascontiguousarray(index, dtype="<i8").tobytes())
    digest.update(np.ascontiguousarray(values).tobytes())
    return digest.hexdigest()[:16]


def code_version() -> dict[str, Any]:
    """Git commit of the running code.

    ``QTS_GIT_COMMIT`` (set when the Docker image is built) wins; otherwise the
    repository the package is running from is asked. ``dirty`` tells whether
    there were uncommitted changes — a result from a dirty tree is flagged.
    """
    commit = os.environ.get("QTS_GIT_COMMIT", "").strip()
    dirty: bool | None = None
    if not commit or commit == "unknown":
        commit, dirty = _git_state(Path(__file__).resolve().parent)
    return {
        "git_commit": commit or "unknown",
        "git_dirty": dirty,
        "qts": __version__,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
    }


def _git_state(directory: Path) -> tuple[str, bool | None]:
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", *args],  # noqa: S607 - git from PATH is the point
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if not commit:
        return "", None
    status = git("status", "--porcelain", "--untracked-files=no")
    return commit, (bool(status) if status is not None else None)


def build_provenance(
    config: dict[str, Any],
    fingerprint: str,
    data: dict[str, pd.DataFrame],
    equity: pd.Series,
    strategy_name: str,
    strategy_version: str,
) -> dict[str, Any]:
    """Assemble the provenance record of one run."""
    code = code_version()
    pinned = bool(config.get("data", {}).get("end"))
    return {
        "period": {
            "start": equity.index[0].isoformat(),
            "end": equity.index[-1].isoformat(),
            "bars": len(equity),
            # False when data.end is open: the same command tomorrow covers one more day
            "pinned": pinned,
        },
        "symbols": list(config.get("market", {}).get("symbols", sorted(data))),
        "timeframe": config.get("market", {}).get("timeframe"),
        "strategy": {
            "name": strategy_name,
            "version": strategy_version,
            "params": config.get("strategy", {}).get("params", {}),
        },
        "costs": {
            "fee_rate": config.get("costs", {}).get("fee_rate"),
            "slippage_bps": config.get("costs", {}).get("slippage_bps"),
        },
        "initial_capital": config.get("portfolio", {}).get("initial_capital"),
        "config_fingerprint": fingerprint,
        "data_version": data_version(data),
        "code": code,
        "result_hash": result_hash(equity),
        "reproducible": pinned and code["git_commit"] != "unknown" and not code["git_dirty"],
    }
