"""Building a tradable universe without hindsight.

Two separate questions:

1. **Which instruments exist at all?** Everything the archive ever listed for a
   quote currency, *including what was later delisted*, minus instruments that
   are not an independent spot asset (stablecoins and fiat, leveraged tokens,
   wrapped duplicates). The exclusions are rules about what an instrument *is*,
   never about how it performed.
2. **Which of them could be traded on a given day?** Membership is decided from
   information available on that day only: listed long enough, and liquid enough
   over the trailing window. An asset that is delisted simply stops being a
   member — and whoever held it has to get out (see the execution model).
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

import pandas as pd

from qts.core.errors import QtsError
from qts.core.logging import get_logger
from qts.market_data.sources.binance_vision import BinanceVisionSource
from qts.market_data.store import ParquetStore

log = get_logger("market_data.universe")

#: Bases pegged to a currency or to another listed asset: not an independent bet.
STABLE_OR_FIAT = frozenset(
    {
        "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDS", "SUSD", "UST", "USTC",
        "USDD", "EUR", "EURI", "GBP", "AUD", "BRL", "RUB", "TRY", "UAH", "NGN", "ZAR", "BIDR",
        "IDRT", "BKRW", "BVND", "VAI", "AEUR", "XUSD", "USD1", "PYUSD", "USDE", "EURC",
    }
)  # fmt: skip
WRAPPED = frozenset({"WBTC", "WBETH", "BETH", "BNSOL"})
#: Leveraged tokens (BTCUP, ETHDOWN, …BULL, …BEAR): decaying derivatives, not spot assets.
_LEVERAGED = re.compile(r".*(UP|DOWN|BULL|BEAR)$")
#: Real assets whose ticker happens to end like a leveraged token.
_NOT_LEVERAGED = frozenset({"JUP", "SYRUP"})


def classify(symbol: str) -> str | None:
    """Reason a symbol is excluded from the research universe, or ``None`` if it is eligible."""
    base = symbol.split("/")[0]
    if base in STABLE_OR_FIAT:
        return "stablecoin or fiat"
    if base in WRAPPED:
        return "wrapped duplicate of another asset"
    if _LEVERAGED.fullmatch(base) and base not in _NOT_LEVERAGED:
        return "leveraged token"
    return None


@dataclass(frozen=True)
class DownloadResult:
    symbol: str
    rows: int
    first: pd.Timestamp | None
    last: pd.Timestamp | None
    error: str | None = None


def download_symbol(
    source: BinanceVisionSource,
    store: ParquetStore,
    exchange: str,
    symbol: str,
    timeframe: str,
    end: date,
) -> DownloadResult:
    """Download (or top up) one symbol. Never raises: a symbol that cannot be
    fetched or fails the quality checks is a recorded result, so one bad
    instrument does not stop a download of hundreds."""
    try:
        after = None
        if store.exists(exchange, symbol, timeframe):
            stored = store.read(exchange, symbol, timeframe)
            if not stored.empty:
                after = stored.index[-1].date()
        frame = source.fetch_listed(symbol, timeframe, end, after)
        if not frame.empty:
            store.write(exchange, symbol, timeframe, frame)
        if not store.exists(exchange, symbol, timeframe):
            return DownloadResult(symbol, 0, None, None, "no data in the archive")
        full = store.read(exchange, symbol, timeframe)
        return DownloadResult(symbol, len(full), full.index[0], full.index[-1])
    except QtsError as exc:
        return DownloadResult(symbol, 0, None, None, str(exc)[:300])


def download_universe(
    source: BinanceVisionSource,
    store: ParquetStore,
    exchange: str,
    symbols: list[str],
    timeframe: str,
    end: date,
    workers: int = 8,
) -> list[DownloadResult]:
    """Download many symbols concurrently. Resumable: stored months are skipped."""

    def one(symbol: str) -> DownloadResult:
        result = download_symbol(source, store, exchange, symbol, timeframe, end)
        log.info(
            "universe_symbol",
            symbol=symbol,
            rows=result.rows,
            error=result.error,
            timeframe=timeframe,
        )
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, symbols))
