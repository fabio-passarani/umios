"""Data-quality audit of a whole universe.

``schema.check_quality`` decides whether one frame is usable at all. This module
describes *how good* the data of every instrument is, so that problems are dealt
with by explicit rules (see ``universe.UniverseRules``) instead of being
discovered through a strange backtest:

* holes in the series — including long ones where a ticker was suspended, or
  retired and later **reused for a different asset**;
* candles without trades, flat candles, absurd wicks and one-candle moves;
* when each instrument started and stopped trading (delistings).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
import numpy as np
import pandas as pd

from qts.market_data.schema import bar_duration

#: Public market-data endpoint (no key, no account): what a running system sees.
LIVE_API = "https://data-api.binance.vision/api/v3"
#: A close-to-close move beyond these is flagged (not removed): real crashes and
#: listing-day spikes exist, but each one deserves a look.
EXTREME_UP = 2.0  # +200%
EXTREME_DOWN = -0.7  # -70%
#: High more than this multiple of the candle's body top: a "flash" wick.
WICK_MULTIPLE = 3.0


@dataclass(frozen=True)
class Gap:
    after: pd.Timestamp
    before: pd.Timestamp
    missing: int


@dataclass
class SymbolQuality:
    symbol: str
    rows: int
    first: pd.Timestamp
    last: pd.Timestamp
    missing: int
    gaps: list[Gap] = field(default_factory=list)
    zero_volume: int = 0
    flat: int = 0
    extreme_up: int = 0
    extreme_down: int = 0
    wicks: int = 0
    has_liquidity: bool = False
    median_quote_volume: float = float("nan")
    delisted: bool = False

    @property
    def longest_gap(self) -> int:
        return max((g.missing for g in self.gaps), default=0)

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["longest_gap"] = self.longest_gap
        return raw


def audit_symbol(
    frame: pd.DataFrame, symbol: str, timeframe: str, dataset_end: pd.Timestamp
) -> SymbolQuality:
    """Describe one instrument's data. ``dataset_end`` is the last candle the
    dataset could contain: an instrument that stops well before it was delisted."""
    step = bar_duration(timeframe)
    index = pd.DatetimeIndex(frame.index).as_unit("ns")
    stamps = index.asi8  # nanoseconds
    jumps = np.diff(stamps)
    gaps = [
        Gap(after=index[i], before=index[i + 1], missing=int(jumps[i] // step.value) - 1)
        for i in map(int, np.flatnonzero(jumps > step.value))
    ]
    close = frame["close"]
    change = close.pct_change()
    body_top = frame[["open", "close"]].max(axis=1)
    liquidity = "quote_volume" in frame.columns
    return SymbolQuality(
        symbol=symbol,
        rows=len(frame),
        first=index[0],
        last=index[-1],
        missing=sum(g.missing for g in gaps),
        gaps=gaps,
        zero_volume=int((frame["volume"] == 0).sum()),
        flat=int((frame["high"] == frame["low"]).sum()),
        extreme_up=int((change > EXTREME_UP).sum()),
        extreme_down=int((change < EXTREME_DOWN).sum()),
        wicks=int((frame["high"] / body_top > WICK_MULTIPLE).sum()),
        has_liquidity=liquidity,
        median_quote_volume=float(np.nanmedian(frame["quote_volume"]))
        if liquidity
        else float("nan"),
        delisted=bool(index[-1] < dataset_end - pd.Timedelta(days=7)),
    )


def audit_universe(
    data: dict[str, pd.DataFrame], timeframe: str, dataset_end: pd.Timestamp
) -> list[SymbolQuality]:
    return [
        audit_symbol(frame, symbol, timeframe, dataset_end)
        for symbol, frame in sorted(data.items())
    ]


def summarise(audits: list[SymbolQuality]) -> dict[str, Any]:
    """Universe-level totals for the report."""
    listed = Counter(a.first.year for a in audits)
    gone = Counter(a.last.year for a in audits if a.delisted)
    return {
        "symbols": len(audits),
        "candles": sum(a.rows for a in audits),
        "delisted": sum(a.delisted for a in audits),
        "with_gaps": sum(bool(a.gaps) for a in audits),
        "missing_candles": sum(a.missing for a in audits),
        "long_gaps": sum(a.longest_gap > 7 for a in audits),
        "zero_volume_candles": sum(a.zero_volume for a in audits),
        "flat_candles": sum(a.flat for a in audits),
        "extreme_up": sum(a.extreme_up for a in audits),
        "extreme_down": sum(a.extreme_down for a in audits),
        "wicks": sum(a.wicks for a in audits),
        "without_liquidity_columns": sum(not a.has_liquidity for a in audits),
        "listed_by_year": dict(sorted(listed.items())),
        "delisted_by_year": dict(sorted(gone.items())),
    }


def compare_with_live(
    frame: pd.DataFrame,
    symbol: str,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Compare stored (archive) candles with what the exchange's public market-data
    API returns for the same period.

    Backtests run on the archive; paper trading and any future live system see
    the API. If the two disagreed, a backtest would describe a market that the
    running system never sees. Returns the number of candles compared and the
    largest relative difference per field.
    """
    http = client or httpx.Client(timeout=30.0)
    params: dict[str, str | int] = {
        "symbol": symbol.replace("/", ""),
        "interval": timeframe,
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "limit": 1000,
    }
    response = http.get(f"{LIVE_API}/klines", params=params)
    response.raise_for_status()
    rows = response.json()
    live = pd.DataFrame(
        [[float(v) for v in row[1:6]] for row in rows],
        index=pd.DatetimeIndex([pd.Timestamp(row[0], unit="ms", tz="UTC") for row in rows]),
        columns=["open", "high", "low", "close", "volume"],
    )
    stored = frame.loc[(frame.index >= start) & (frame.index <= end), list(live.columns)]
    common = stored.index.intersection(live.index)
    difference = (stored.loc[common] / live.loc[common] - 1.0).abs().max()
    return {
        "symbol": symbol,
        "stored": len(stored),
        "live": len(live),
        "compared": len(common),
        "only_in_archive": len(stored.index.difference(live.index)),
        "only_in_live": len(live.index.difference(stored.index)),
        "max_relative_difference": {k: float(v) for k, v in difference.items()},
    }
