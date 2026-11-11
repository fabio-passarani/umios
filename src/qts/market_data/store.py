"""Parquet storage for historical candles.

Parquet is used for history (columnar, compressed, typed, readable by pandas,
DuckDB, Polars…) while PostgreSQL holds *operational* state. One file per
``exchange/symbol/timeframe`` keeps reads trivial at daily resolution.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from qts.core.errors import DataError
from qts.market_data.schema import normalize_ohlcv, require_quality


class ParquetStore:
    """Reads and writes canonical OHLCV frames under ``<root>/ohlcv``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, exchange: str, symbol: str, timeframe: str) -> Path:
        safe_symbol = symbol.replace("/", "_").upper()
        return self.root / "ohlcv" / exchange.lower() / safe_symbol / f"{timeframe}.parquet"

    def exists(self, exchange: str, symbol: str, timeframe: str) -> bool:
        return self.path(exchange, symbol, timeframe).is_file()

    def write(self, exchange: str, symbol: str, timeframe: str, frame: pd.DataFrame) -> Path:
        """Merge ``frame`` into the stored history (new rows win) and write atomically."""
        new = normalize_ohlcv(frame)
        target = self.path(exchange, symbol, timeframe)
        if target.is_file():
            new = normalize_ohlcv(pd.concat([self._read_raw(target), new]))
        require_quality(new, symbol, timeframe)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".parquet.tmp")
        new.to_parquet(tmp, engine="pyarrow", compression="zstd")
        os.replace(tmp, target)  # atomic: readers never see a half-written file
        return target

    @staticmethod
    def _read_raw(path: Path) -> pd.DataFrame:
        return normalize_ohlcv(pd.read_parquet(path, engine="pyarrow"))

    def read(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Load stored candles, optionally restricted to ``[start, end]``."""
        target = self.path(exchange, symbol, timeframe)
        if not target.is_file():
            raise DataError(
                f"no stored data for {exchange} {symbol} {timeframe}: run `qts data download`"
            )
        df = self._read_raw(target)
        if start is not None:
            df = df.loc[df.index >= _utc(start)]
        if end is not None:
            df = df.loc[df.index <= _utc(end)]
        return df


def _utc(value: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
