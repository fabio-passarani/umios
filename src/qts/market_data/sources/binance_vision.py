"""Binance public data archive (https://data.binance.vision).

Why this source for history instead of the REST API:

* it is the exchange's own official bulk archive, free and unauthenticated —
  no API key is ever needed to research;
* every file ships with a SHA-256 checksum, so downloads are verifiable;
* files are immutable, which makes backtests reproducible.

Layout: one zip per symbol, timeframe and month (``monthly``) plus one per day
(``daily``) for the month still in progress. Timestamps are milliseconds until
2024 and microseconds from 2025: both are handled.

The archive keeps the files of symbols that were later **delisted**, and its
index can be listed: :meth:`BinanceVisionSource.list_symbols` returns every
pair that ever traded, which is what a survivorship-free universe needs.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

import httpx
import pandas as pd

from qts.core.errors import DataError
from qts.core.logging import get_logger
from qts.market_data.schema import bar_duration, normalize_ohlcv
from qts.market_data.sources.base import MarketDataSource

log = get_logger("market_data.binance_vision")

BASE_URL = "https://data.binance.vision/data/spot"
INDEX_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
TIMEFRAMES = ("1h", "4h", "1d")
_KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]
#: Positions of the liquidity fields in a full kline row (after the six above):
#: close_time, quote_volume, trades, taker_buy_base, taker_buy_quote, ignore.
_LIQUIDITY_FIELDS = {"quote_volume": 7, "trades": 8, "taker_buy_quote": 10}


def _month_starts(start: date, end: date) -> list[date]:
    months: list[date] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def parse_klines_csv(raw: bytes) -> pd.DataFrame:
    """Parse one kline CSV (no header, or with a header in some newer files)."""
    full = pd.read_csv(io.BytesIO(raw), header=None)
    if full.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    if isinstance(full.iloc[0, 0], str) and not str(full.iloc[0, 0]).isdigit():
        full = full.iloc[1:]
    df = full.iloc[:, :6].copy()
    df.columns = _KLINE_COLUMNS
    open_time = pd.to_numeric(df["open_time"]).astype("int64")
    # ms until 2024, microseconds from 2025-01-01 onwards
    unit = open_time.map(lambda v: "us" if v > 10**14 else "ms")
    timestamps = [pd.Timestamp(v, unit=u, tz="UTC") for v, u in zip(open_time, unit, strict=True)]
    out = df[["open", "high", "low", "close", "volume"]].astype("float64").reset_index(drop=True)
    for name, position in _LIQUIDITY_FIELDS.items():
        if full.shape[1] > position:  # older or reduced files may lack them
            out[name] = pd.to_numeric(full.iloc[:, position]).astype("float64").to_numpy()
    out.insert(0, "timestamp", pd.DatetimeIndex(timestamps))
    return out


def drop_off_grid(df: pd.DataFrame, timeframe: str, symbol: str) -> pd.DataFrame:
    """Remove candles whose open time is not on the timeframe's grid.

    The archive contains a few: after the exchange's outage of 8-9 February
    2018 some hourly candles of the oldest pairs open 28 minutes late. They are
    dropped — and become ordinary missing candles — rather than moved onto the
    grid, which would invent candles that never existed.
    """
    step = bar_duration(timeframe).value
    on_grid = pd.DatetimeIndex(df.index).as_unit("ns").asi8 % step == 0
    dropped = int((~on_grid).sum())
    if dropped:
        log.warning("off_grid_candles_dropped", symbol=symbol, timeframe=timeframe, count=dropped)
    kept: pd.DataFrame = df.loc[on_grid]
    return kept


class BinanceVisionSource(MarketDataSource):
    """Downloads spot klines from the Binance public archive with checksum verification."""

    name = "binance"

    def __init__(self, client: httpx.Client | None = None, verify_checksum: bool = True) -> None:
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._verify_checksum = verify_checksum

    @staticmethod
    def _pair(symbol: str) -> str:
        return symbol.replace("/", "").upper()

    def _get(self, url: str) -> bytes | None:
        """GET with small retry; ``None`` when the file does not exist (404)."""
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if response.status_code == 404:
                return None
            if response.status_code == 200:
                return response.content
            last_error = DataError(f"HTTP {response.status_code} for {url}")
        raise DataError(f"download failed for {url}: {last_error}")

    def _download_zip(self, url: str) -> pd.DataFrame | None:
        payload = self._get(url)
        if payload is None:
            return None
        if self._verify_checksum:
            checksum = self._get(url + ".CHECKSUM")
            if checksum is None:
                raise DataError(f"checksum file missing for {url}")
            expected = checksum.decode().split()[0].strip().lower()
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise DataError(f"checksum mismatch for {url}")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise DataError(f"unexpected archive content in {url}: {names}")
            return parse_klines_csv(archive.read(names[0]))

    # ----------------------------------------------------------------- index
    def _list(self, prefix: str, folders: bool) -> list[str]:
        """Names directly under ``prefix`` in the archive index (paginated)."""
        names: list[str] = []
        marker = ""
        pattern = re.compile(
            (r"<Prefix>" if folders else r"<Key>") + re.escape(prefix) + r"([^<]+?)/?</"
        )
        while True:
            params = {"prefix": prefix, "marker": marker}
            if folders:
                params["delimiter"] = "/"
            page = self._get(f"{INDEX_URL}?{urlencode(params)}")
            if page is None:
                raise DataError("the archive index is not available")
            text = page.decode("utf-8", errors="replace")
            names += [n for n in pattern.findall(text) if n]
            more = re.search(r"<NextMarker>([^<]+)</NextMarker>", text)
            if "<IsTruncated>true</IsTruncated>" not in text:
                return names
            if more:
                marker = more.group(1)
            elif names:
                marker = prefix + names[-1]
            else:  # pragma: no cover - a truncated page always carries entries
                return names

    def list_symbols(self, quote: str = "USDT") -> list[str]:
        """Every ``BASE/QUOTE`` pair that has ever had monthly candles in the
        archive — including pairs that were delisted since."""
        folders = self._list("data/spot/monthly/klines/", folders=True)
        suffix = quote.upper()
        return sorted(
            f"{name[: -len(suffix)]}/{suffix}"
            for name in folders
            if name.endswith(suffix) and len(name) > len(suffix)
        )

    def list_months(self, symbol: str, timeframe: str) -> list[date]:
        """Months for which the archive holds a complete monthly file."""
        pair = self._pair(symbol)
        prefix = f"data/spot/monthly/klines/{pair}/{timeframe}/"
        months = []
        for key in self._list(prefix, folders=False):
            found = re.fullmatch(rf"{pair}-{timeframe}-(\d{{4}})-(\d{{2}})\.zip", key)
            if found:
                months.append(date(int(found.group(1)), int(found.group(2)), 1))
        return sorted(months)

    def fetch_listed(
        self, symbol: str, timeframe: str, end: date, after: date | None = None
    ) -> pd.DataFrame:
        """Whole archived history of ``symbol`` up to ``end``, driven by the index
        (no probing of months that do not exist). ``after`` skips months that
        are already stored. Used for bulk downloads of a large universe."""
        if timeframe not in TIMEFRAMES:
            raise DataError(f"timeframe {timeframe!r} is not supported")
        pair = self._pair(symbol)
        months = [m for m in self.list_months(symbol, timeframe) if m <= end]
        frames: list[pd.DataFrame] = []
        for month in months:
            if after is not None and month < after.replace(day=1):
                continue
            name = f"{pair}-{timeframe}-{month:%Y-%m}.zip"
            frame = self._download_zip(f"{BASE_URL}/monthly/klines/{pair}/{timeframe}/{name}")
            if frame is not None:
                frames.append(frame)
        # days after the last complete month (only if the symbol is still trading then)
        if months:
            day = (months[-1].replace(day=28) + timedelta(days=4)).replace(day=1)
            misses = 0
            while day <= end and misses < 3:
                name = f"{pair}-{timeframe}-{day:%Y-%m-%d}.zip"
                frame = self._download_zip(f"{BASE_URL}/daily/klines/{pair}/{timeframe}/{name}")
                if frame is None:
                    misses += 1
                else:
                    misses = 0
                    frames.append(frame)
                day += timedelta(days=1)
        if not frames:
            return pd.DataFrame()
        df = drop_off_grid(normalize_ohlcv(pd.concat(frames, ignore_index=True)), timeframe, symbol)
        return df.loc[df.index < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)]

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
        if timeframe not in TIMEFRAMES:
            raise DataError(
                f"timeframe {timeframe!r} is not supported (available: {', '.join(TIMEFRAMES)})"
            )
        # Only closed candles: today's daily candle is still forming.
        last_closed = datetime.now(tz=UTC).date() - timedelta(days=1)
        end = min(end, last_closed)
        pair = self._pair(symbol)
        frames: list[pd.DataFrame] = []
        for month in _month_starts(start, end):
            url = (
                f"{BASE_URL}/monthly/klines/{pair}/{timeframe}/{pair}-{timeframe}-{month:%Y-%m}.zip"
            )
            monthly = self._download_zip(url)
            if monthly is not None:
                frames.append(monthly)
                continue
            # Month not archived (still in progress, or before listing): try daily files.
            day = max(month, start)
            month_end = min(
                (month.replace(day=28) + timedelta(days=4)).replace(day=1), end + timedelta(days=1)
            )
            recent = (last_closed - month).days <= 62
            while recent and day < month_end:
                name = f"{pair}-{timeframe}-{day:%Y-%m-%d}.zip"
                url = f"{BASE_URL}/daily/klines/{pair}/{timeframe}/{name}"
                daily = self._download_zip(url)
                if daily is not None:
                    frames.append(daily)
                day += timedelta(days=1)
        if not frames:
            raise DataError(f"no data found for {symbol} between {start} and {end}")
        df = drop_off_grid(normalize_ohlcv(pd.concat(frames, ignore_index=True)), timeframe, symbol)
        lo = pd.Timestamp(start, tz="UTC")
        hi = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        df = df.loc[(df.index >= lo) & (df.index < hi)]
        log.info("ohlcv_downloaded", symbol=symbol, rows=len(df), source=self.name)
        return df
