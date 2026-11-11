"""Interface every historical data source implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


class MarketDataSource(ABC):
    """Fetches *closed* candles and returns them in the canonical OHLCV schema.

    This is the extension point for new markets: an Interactive Brokers or
    equities/ETF source only has to implement :meth:`fetch_ohlcv`.
    """

    name: str

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
        """Return candles with ``start <= open time <= end`` (canonical schema)."""
