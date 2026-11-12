import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pandas as pd
import pytest

from qts.core.config import AppConfig
from qts.core.errors import DataError
from qts.market_data.schema import check_quality, normalize_ohlcv
from qts.market_data.service import load_universe, update_history
from qts.market_data.sources.base import MarketDataSource
from qts.market_data.sources.binance_vision import BinanceVisionSource, parse_klines_csv
from qts.market_data.store import ParquetStore
from tests.conftest import make_candles

MS_ROW = "1704067200000,42283.58,44184.10,42180.77,44179.55,27174.29,1704153599999,1,1,1,1,0\n"
US_ROW = "1735689600000000,93576.0,95151.15,92888.0,94591.79,10373.3,1735775999999999,1,1,1,1,0\n"


def _zip(csv: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("x.csv", csv)
    return buffer.getvalue()


def test_parse_handles_millisecond_and_microsecond_timestamps() -> None:
    frame = parse_klines_csv((MS_ROW + US_ROW).encode())
    assert list(frame["timestamp"]) == [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2025-01-01", tz="UTC"),
    ]
    assert frame["close"].tolist() == [44179.55, 94591.79]


def test_parse_skips_header_row() -> None:
    frame = parse_klines_csv(
        ("open_time,open,high,low,close,volume,a,b,c,d,e,f\n" + MS_ROW).encode()
    )
    assert len(frame) == 1


def _source(files: dict[str, bytes], corrupt_checksum: bool = False) -> BinanceVisionSource:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".CHECKSUM"):
            payload = files.get(url.removesuffix(".CHECKSUM"))
            if payload is None:
                return httpx.Response(404)
            digest = "0" * 64 if corrupt_checksum else hashlib.sha256(payload).hexdigest()
            return httpx.Response(200, text=f"{digest}  file.zip\n")
        if url in files:
            return httpx.Response(200, content=files[url])
        return httpx.Response(404)

    return BinanceVisionSource(client=httpx.Client(transport=httpx.MockTransport(handler)))


BASE = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-"


def test_fetch_assembles_months_and_skips_missing_ones() -> None:
    source = _source({BASE + "2024-01.zip": _zip(MS_ROW), BASE + "2025-01.zip": _zip(US_ROW)})
    frame = source.fetch_ohlcv("BTC/USDT", "1d", date(2023, 11, 1), date(2025, 1, 31))
    assert len(frame) == 2
    assert frame.index.tz is not None
    assert list(frame.columns)[:5] == ["open", "high", "low", "close", "volume"]
    assert set(frame.columns[5:]) <= {"quote_volume", "trades", "taker_buy_quote"}


def test_fetch_rejects_checksum_mismatch() -> None:
    source = _source({BASE + "2024-01.zip": _zip(MS_ROW)}, corrupt_checksum=True)
    with pytest.raises(DataError, match="checksum mismatch"):
        source.fetch_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 1, 31))


def test_fetch_without_any_data_raises() -> None:
    with pytest.raises(DataError, match="no data found"):
        _source({}).fetch_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 1, 31))


def test_unsupported_timeframe() -> None:
    with pytest.raises(DataError, match="not supported"):
        _source({}).fetch_ohlcv("BTC/USDT", "5m", date(2024, 1, 1), date(2024, 1, 31))


def test_normalize_sorts_dedupes_and_localizes() -> None:
    raw = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-01", "2024-01-02"]),
            "open": [2, 1, 3],
            "high": [2, 1, 3],
            "low": [2, 1, 3],
            "close": [2, 1, 3],
            "volume": [1, 1, 1],
            "junk": [0, 0, 0],
        }
    )
    frame = normalize_ohlcv(raw)
    assert frame.index.is_monotonic_increasing
    assert str(frame.index.tz) == "UTC"
    assert frame["close"].tolist() == [1.0, 3.0]  # last duplicate wins
    assert "junk" not in frame.columns


def test_normalize_rejects_missing_columns() -> None:
    with pytest.raises(DataError, match="missing columns"):
        normalize_ohlcv(pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01"]), "open": [1]}))


def test_quality_flags_bad_candles_and_reports_gaps() -> None:
    good = make_candles([10, 11, 12, 13])
    assert check_quality(good).ok
    gap = good.drop(good.index[1])
    report = check_quality(gap)
    assert report.ok
    assert report.missing_candles == 1
    broken = good.copy()
    broken.iloc[1, broken.columns.get_loc("high")] = 0.5
    assert not check_quality(broken).ok
    negative = good.copy()
    negative.iloc[0, negative.columns.get_loc("close")] = -1
    assert not check_quality(negative).ok
    assert not check_quality(good.iloc[0:0]).ok


def test_store_roundtrip_merge_and_window(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    first = make_candles([10, 11, 12], start="2024-01-01")
    store.write("binance", "BTC/USDT", "1d", first)
    update = make_candles([12.5, 13, 14], start="2024-01-03")  # overlaps the last day
    path = store.write("binance", "BTC/USDT", "1d", update)
    assert path == tmp_path / "ohlcv" / "binance" / "BTC_USDT" / "1d.parquet"
    stored = store.read("binance", "BTC/USDT", "1d")
    assert len(stored) == 5
    assert stored.loc["2024-01-03", "close"] == 12.5  # newer download wins
    window = store.read("binance", "BTC/USDT", "1d", start="2024-01-02", end="2024-01-04")
    assert len(window) == 3
    assert not list(tmp_path.rglob("*.tmp"))


def test_store_refuses_corrupt_data_and_missing_symbol(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    bad = make_candles([10, 11])
    bad.iloc[0, bad.columns.get_loc("low")] = 99
    with pytest.raises(DataError, match="quality"):
        store.write("binance", "BTC/USDT", "1d", bad)
    with pytest.raises(DataError, match="no stored data"):
        store.read("binance", "ETH/USDT", "1d")


class _FakeSource(MarketDataSource):
    name = "binance"

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[str, date, date]] = []

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append((symbol, start, end))
        lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
        return self.frame.loc[(self.frame.index >= lo) & (self.frame.index <= hi)]


def test_update_history_is_incremental_and_idempotent(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "market": {"symbols": ["BTC/USDT"]},
            "data": {"directory": str(tmp_path), "start": "2024-01-01", "end": "2024-03-10"},
            "risk": {"allowed_symbols": None},
        }
    )
    candles = make_candles(range(100, 170), start="2024-01-01")  # through 2024-03-10
    store = ParquetStore(tmp_path)
    source = _FakeSource(candles.iloc[:45])  # history published so far
    first = update_history(config, source, store)
    assert first["BTC/USDT"].rows == 45
    assert source.calls[0][1] == date(2024, 1, 1)

    source.frame = candles
    second = update_history(config, source, store)
    assert second["BTC/USDT"].rows == 70
    assert second["BTC/USDT"].missing_candles == 0
    assert source.calls[1][1] == date(2024, 2, 1)  # only re-fetches from the last stored month
    assert update_history(config, source, store)["BTC/USDT"].rows == 70
    loaded = load_universe(config, store)["BTC/USDT"]
    assert loaded["close"].tolist() == candles["close"].tolist()


def test_off_grid_candles_are_dropped_not_realigned() -> None:
    """Real case: hourly candles that open 28 minutes late after an exchange outage."""
    from qts.market_data.sources.binance_vision import drop_off_grid

    index = pd.DatetimeIndex(
        [
            "2018-02-08 00:00",
            "2018-02-08 01:00",
            "2018-02-09 09:28:14.789",
            "2018-02-09 10:28:14.789",
            "2018-02-10 00:00",
        ],
        tz="UTC",
        name="timestamp",
    )
    frame = pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}, index=index
    )
    cleaned = drop_off_grid(frame, "1h", "BTC/USDT")
    assert list(cleaned.index) == [index[0], index[1], index[4]]  # nothing was moved
    assert check_quality(cleaned, "1h").ok
    assert check_quality(cleaned, "1h").missing_candles > 0  # the hole is visible as such
    assert (
        len(drop_off_grid(frame.iloc[[0, 1, 4]], "1d", "BTC/USDT")) == 2
    )  # 01:00 is off the daily grid
