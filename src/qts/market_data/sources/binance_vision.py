"""Binance public data archive (https://data.binance.vision).

Why this source for history instead of the REST API:

* it is the exchange's own official bulk archive, free and unauthenticated —
  no API key is ever needed to research;
* every file ships with a SHA-256 checksum, so downloads are verifiable;
* files are immutable, which makes backtests reproducible.

Layout: one zip per symbol per month (``monthly``) plus one per day (``daily``)
for the month still in progress. Timestamps are milliseconds until 2024 and
microseconds from 2025: both are handled.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd

from qts.core.errors import DataError
from qts.core.logging import get_logger
from qts.market_data.schema import normalize_ohlcv
from qts.market_data.sources.base import MarketDataSource

log = get_logger("market_data.binance_vision")

BASE_URL = "https://data.binance.vision/data/spot"
_KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]


def _month_starts(start: date, end: date) -> list[date]:
    months: list[date] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def parse_klines_csv(raw: bytes) -> pd.DataFrame:
    """Parse one kline CSV (no header, or with a header in some newer files)."""
    df = pd.read_csv(io.BytesIO(raw), header=None, usecols=range(6), names=_KLINE_COLUMNS)
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    if isinstance(df.iloc[0, 0], str) and not str(df.iloc[0, 0]).isdigit():
        df = df.iloc[1:]
    open_time = pd.to_numeric(df["open_time"]).astype("int64")
    # ms until 2024, microseconds from 2025-01-01 onwards
    unit = open_time.map(lambda v: "us" if v > 10**14 else "ms")
    timestamps = [pd.Timestamp(v, unit=u, tz="UTC") for v, u in zip(open_time, unit, strict=True)]
    out = df[["open", "high", "low", "close", "volume"]].astype("float64").reset_index(drop=True)
    out.insert(0, "timestamp", pd.DatetimeIndex(timestamps))
    return out


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

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
        if timeframe != "1d":
            raise DataError(f"timeframe {timeframe!r} is not supported yet (MVP: 1d)")
        # Only closed candles: today's daily candle is still forming.
        last_closed = datetime.now(tz=UTC).date() - timedelta(days=1)
        end = min(end, last_closed)
        pair = self._pair(symbol)
        frames: list[pd.DataFrame] = []
        for month in _month_starts(start, end):
            url = f"{BASE_URL}/monthly/klines/{pair}/1d/{pair}-1d-{month:%Y-%m}.zip"
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
                url = f"{BASE_URL}/daily/klines/{pair}/1d/{pair}-1d-{day:%Y-%m-%d}.zip"
                daily = self._download_zip(url)
                if daily is not None:
                    frames.append(daily)
                day += timedelta(days=1)
        if not frames:
            raise DataError(f"no data found for {symbol} between {start} and {end}")
        df = normalize_ohlcv(pd.concat(frames, ignore_index=True))
        lo = pd.Timestamp(start, tz="UTC")
        hi = pd.Timestamp(end, tz="UTC")
        df = df.loc[(df.index >= lo) & (df.index <= hi)]
        log.info("ohlcv_downloaded", symbol=symbol, rows=len(df), source=self.name)
        return df
