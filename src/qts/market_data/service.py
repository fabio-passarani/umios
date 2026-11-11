"""High-level data operations used by the CLI and by paper trading."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from qts.core.config import AppConfig
from qts.core.logging import get_logger
from qts.market_data.schema import QualityReport, require_quality
from qts.market_data.sources.base import MarketDataSource
from qts.market_data.store import ParquetStore

log = get_logger("market_data.service")


def update_history(
    config: AppConfig, source: MarketDataSource, store: ParquetStore
) -> dict[str, QualityReport]:
    """Download (or incrementally refresh) every configured symbol into the store."""
    reports: dict[str, QualityReport] = {}
    market = config.market
    end = date.fromisoformat(config.data.end) if config.data.end else datetime.now(tz=UTC).date()
    for symbol in market.symbols:
        start = date.fromisoformat(config.data.start)
        if store.exists(market.exchange, symbol, market.timeframe):
            stored = store.read(market.exchange, symbol, market.timeframe)
            if not stored.empty:
                # Re-fetch from the start of the last stored month: cheap and idempotent.
                start = max(start, stored.index[-1].date().replace(day=1))
        frame = source.fetch_ohlcv(symbol, market.timeframe, start, end)
        store.write(market.exchange, symbol, market.timeframe, frame)
        full = store.read(market.exchange, symbol, market.timeframe)
        reports[symbol] = require_quality(full, symbol, market.timeframe)
        for warning in reports[symbol].warnings:
            log.warning("data_quality_warning", symbol=symbol, detail=warning)
    return reports


def load_universe(config: AppConfig, store: ParquetStore) -> dict[str, pd.DataFrame]:
    """Load the configured symbols for the configured period."""
    market = config.market
    return {
        symbol: store.read(
            market.exchange, symbol, market.timeframe, config.data.start, config.data.end
        )
        for symbol in market.symbols
    }
