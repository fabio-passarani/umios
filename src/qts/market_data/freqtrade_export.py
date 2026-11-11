"""Export stored candles to Freqtrade's on-disk format.

Freqtrade reads ``<datadir>/<PAIR>-<timeframe>.feather`` with columns
``date, open, high, low, close, volume``. Exporting from our Parquet store means
both engines backtest on byte-identical data.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qts.core.config import AppConfig
from qts.market_data.store import ParquetStore


def export_to_freqtrade(config: AppConfig, store: ParquetStore, datadir: Path) -> list[Path]:
    """Write one feather file per configured symbol; returns the written paths."""
    market = config.market
    datadir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for symbol in market.symbols:
        frame = store.read(market.exchange, symbol, market.timeframe)
        out = frame.reset_index().rename(columns={"timestamp": "date"})
        out["date"] = pd.DatetimeIndex(out["date"]).as_unit("ms")
        path = datadir / f"{symbol.replace('/', '_')}-{market.timeframe}.feather"
        out.to_feather(path, compression="lz4", compression_level=9)
        written.append(path)
    return written
