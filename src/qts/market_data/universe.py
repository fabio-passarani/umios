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

import numpy as np
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
        "USDSOLD", "USDSB", "USDEB",
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


# --------------------------------------------------------------------- membership
@dataclass(frozen=True)
class UniverseRules:
    """Who may be traded on a given day — decided from that day's past only.

    The defaults are stated reasons, not tuned numbers:

    * ``min_history_days`` 90 — a strategy needs indicator history, and the first
      weeks after a listing are dominated by spikes that no one could trade at
      the printed prices;
    * ``max_gap_days`` 3 — a longer hole means the instrument was suspended or
      the ticker was retired (and sometimes reused for a different asset): its
      history restarts from zero after the hole;
    * ``liquidity_window_days`` 30 and ``min_quote_volume`` 1 000 000 — the median
      traded value per day must make a small account's order a negligible share
      of the market;
    * ``top_n`` 30 — the most liquid names among those, so the universe has a
      stable size through time instead of growing with the number of listings;
    * ``min_daily_volatility`` 0.3% — an instrument whose price barely moves is
      pegged to something (a stablecoin under a name the exclusion list does not
      know). It is a statement about what the instrument is, measured on its
      past, not about whether it went up or down;
    * membership is reviewed on the first day of each month.
    """

    min_history_days: int = 90
    max_gap_days: int = 3
    liquidity_window_days: int = 30
    min_quote_volume: float = 1_000_000.0
    top_n: int = 30
    min_daily_volatility: float = 0.003


def membership(data: dict[str, pd.DataFrame], rules: UniverseRules) -> pd.DataFrame:
    """Boolean table (day x symbol): True when the symbol belongs to the universe.

    Built from **daily** candles. The row of day *D* uses candles up to *D-1*
    only, so it is known before *D* opens. Membership changes on the first day
    of a month and is kept until the next review — except that an instrument
    stops being a member the day after its last candle.
    """
    days = pd.DatetimeIndex(sorted(set().union(*(f.index for f in data.values()))))
    days = pd.date_range(days[0], days[-1], freq="D", tz="UTC")
    value = pd.DataFrame(
        {
            s: f["quote_volume"] if "quote_volume" in f else f["volume"] * f["close"]
            for s, f in data.items()
        }
    ).reindex(days)
    present = value.notna()

    # uninterrupted history: days since the last hole longer than max_gap_days
    age = pd.DataFrame(
        {s: _age(present[s].to_numpy(), rules.max_gap_days) for s in present.columns}, index=days
    )
    median = value.rolling(
        rules.liquidity_window_days, min_periods=rules.liquidity_window_days // 2
    ).median()
    closes = pd.DataFrame({s: f["close"] for s, f in data.items()}).reindex(days)
    volatility = (
        closes.pct_change(fill_method=None)
        .rolling(rules.liquidity_window_days, min_periods=rules.liquidity_window_days // 2)
        .std()
    )

    # everything shifted by one day: the row of D is what was known at the close of D-1
    known_age, known_median, traded_yesterday = age.shift(1), median.shift(1), present.shift(1)
    eligible = (
        (known_age >= rules.min_history_days)
        & (known_median >= rules.min_quote_volume)
        & (volatility.shift(1) >= rules.min_daily_volatility)
        & traded_yesterday.fillna(False).astype(bool)
    )
    rank = known_median.where(eligible).rank(axis=1, ascending=False, method="first")
    chosen = eligible & (rank <= rules.top_n)

    reviews = chosen[days.day == 1]
    held = reviews.reindex(days).ffill().fillna(False).astype(bool)
    # an instrument that stopped trading is out from the day after its last candle
    return held & traded_yesterday.fillna(False).astype(bool)


def _age(present: np.ndarray, max_gap: int) -> np.ndarray:
    """Candles of uninterrupted history at each day (holes up to ``max_gap`` days
    do not reset it, longer ones do)."""
    age = np.zeros(len(present), dtype=np.int64)
    count, hole = 0, 0
    for i, here in enumerate(present):
        if here:
            if hole > max_gap:
                count = 0
            count += 1
            hole = 0
        else:
            hole += 1
        age[i] = count if here or hole <= max_gap else 0
    return age
