"""Where the execution model's estimated coefficients come from.

The data source has **no historical bid/ask and no historical order book** for
spot markets (the public archive has candles and trades, not quotes). What can
be observed is the market *today*: one snapshot of every USDT pair's best bid
and ask, its traded value over the last 24 hours, and — for a sample across the
liquidity range — the full order book, which says what an order of a given size
would have paid to walk it.

From one snapshot two relations are fitted:

* half spread against traded value, ``half_spread_bps = a * V ** b``
  (log-log least squares across pairs);
* price impact beyond the half spread against the square-root law,
  ``impact = k * sigma * sqrt(Q / V)``, where sigma is approximated from the
  day's high-low range (``range / 1.67``) because no return series comes with
  the snapshot.

Both are cross-sections of *one day*. Applying them to 2018-2023 assumes the
relation between liquidity and cost was the same then — it is an assumption,
and the results are therefore scenarios, never measurements. :func:`fit`
reports the dispersion around each fit so that the high-cost scenarios can be
taken from it rather than invented.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

#: Public market-data endpoint (no account, no key).
MARKET_DATA_API = "https://data-api.binance.vision/api/v3"
#: Order sizes, in quote currency, priced against the book.
BOOK_SIZES: tuple[int, ...] = (1_000, 10_000, 100_000)
#: Ratio of the expected daily high-low range to the daily volatility (approximation).
RANGE_TO_SIGMA = 1.67
#: Liquidity buckets of 24h traded value used in reports.
BUCKETS: tuple[float, ...] = (0.0, 1e5, 1e6, 1e7, 1e8, 1e9, 1e13)
BUCKET_LABELS: tuple[str, ...] = ("<100k", "100k-1M", "1M-10M", "10M-100M", "100M-1B", ">1B")


def walk_book(levels: list[tuple[float, float]], value: float, mid: float) -> float:
    """Cost in bps versus ``mid`` of taking ``value`` (quote currency) from ``levels``.

    NaN when the levels do not hold that much value.
    """
    left, paid, quantity = value, 0.0, 0.0
    for price, size in levels:
        take = min(left, price * size)
        paid += take
        quantity += take / price
        left -= take
        if left <= 1e-9:
            return abs(paid / quantity / mid - 1.0) * 1e4
    return math.nan


def take_snapshot(
    client: Any, quote: str = "USDT", sample: int = 50, pause: float = 0.05
) -> dict[str, Any]:
    """Observe the market now: quotes and 24h statistics of every ``quote`` pair,
    order books for the 30 most liquid pairs and ``sample`` more spread evenly
    over the rest of the liquidity range.

    ``client`` is an ``httpx.Client`` (or anything with ``get(url, params=)``).
    Only public market data is read; nothing is sent but the requests themselves.
    """
    taken = datetime.now(tz=UTC).isoformat(timespec="seconds")
    book = {r["symbol"]: r for r in client.get(f"{MARKET_DATA_API}/ticker/bookTicker").json()}
    day = {r["symbol"]: r for r in client.get(f"{MARKET_DATA_API}/ticker/24hr").json()}
    rows: list[dict[str, Any]] = []
    for symbol, quote_row in book.items():
        if not symbol.endswith(quote) or symbol not in day:
            continue
        bid, ask = float(quote_row["bidPrice"]), float(quote_row["askPrice"])
        stats = day[symbol]
        traded = float(stats["quoteVolume"])
        if bid <= 0 or ask <= 0 or traded <= 0:
            continue
        mid = (bid + ask) / 2.0
        rows.append(
            {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "half_spread_bps": (ask - bid) / 2.0 / mid * 1e4,
                "quote_volume_24h": traded,
                "range_pct": (float(stats["highPrice"]) - float(stats["lowPrice"])) / mid,
                "trades": int(stats["count"]),
            }
        )
    rows.sort(key=lambda r: -r["quote_volume_24h"])
    step = max(1, (len(rows) - 30) // max(sample, 1))
    for i in sorted(set(range(min(30, len(rows)))) | set(range(30, len(rows), step))):
        row = rows[i]
        depth = client.get(
            f"{MARKET_DATA_API}/depth", params={"symbol": row["symbol"], "limit": 1000}
        ).json()
        asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
        bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
        if not asks or not bids:
            continue
        mid = (asks[0][0] + bids[0][0]) / 2.0
        for size in BOOK_SIZES:
            row[f"buy_{size}"] = walk_book(asks, size, mid)
            row[f"sell_{size}"] = walk_book(bids, size, mid)
        time.sleep(pause)
    return {"taken_at": taken, "source": MARKET_DATA_API, "quote": quote, "pairs": rows}


def fit(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Fit the spread and impact relations to a snapshot (see the module docstring)."""
    table = pd.DataFrame(snapshot["pairs"])
    x = np.log(table["quote_volume_24h"].to_numpy(float))
    y = np.log(table["half_spread_bps"].clip(lower=1e-4).to_numpy(float))
    slope, intercept = np.polyfit(x, y, 1)
    residual = y - (intercept + slope * x)
    spread = {
        "coefficient_bps": float(math.exp(intercept)),
        "exponent": float(slope),
        "r2": float(np.corrcoef(x, y)[0, 1] ** 2),
        "pairs": len(table),
        # multiplicative dispersion of observed spreads around the fit
        "residual_multiplier": {
            str(q): float(math.exp(np.quantile(residual, q))) for q in (0.1, 0.5, 0.9)
        },
    }

    parts = []
    for size in BOOK_SIZES:
        for side in ("buy", "sell"):
            column = f"{side}_{size}"
            if column not in table:
                continue
            d = table[["quote_volume_24h", "range_pct", "half_spread_bps", column]].dropna()
            extra = (d[column] - d["half_spread_bps"]).clip(lower=0.0) / 1e4
            sigma = d["range_pct"] / RANGE_TO_SIGMA
            scale = sigma * np.sqrt(size / d["quote_volume_24h"])
            parts.append(pd.DataFrame({"extra": extra, "x": scale}))
    impact: dict[str, Any] = {"observations": 0}
    if parts:
        d = pd.concat(parts)
        d = d[d["x"] > 0]
        k = float((d["extra"] * d["x"]).sum() / (d["x"] ** 2).sum())
        ratio = (d["extra"] / d["x"]).replace([np.inf, -np.inf], np.nan).dropna()
        total = float(((d["extra"] - d["extra"].mean()) ** 2).sum())
        impact = {
            "observations": len(d),
            "least_squares_k": k,
            "least_squares_r2": 1.0 - float(((d["extra"] - k * d["x"]) ** 2).sum()) / total
            if total > 0
            else 0.0,
            "k_quantiles": {str(q): float(ratio.quantile(q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)},
        }
    return {"spread": spread, "impact": impact}


def by_liquidity(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Observed half spread and book costs per bucket of 24h traded value."""
    table = pd.DataFrame(snapshot["pairs"])
    table["bucket"] = pd.cut(table["quote_volume_24h"], list(BUCKETS), labels=list(BUCKET_LABELS))
    out = []
    for label, group in table.groupby("bucket", observed=True):
        row: dict[str, Any] = {
            "bucket": str(label),
            "pairs": len(group),
            "half_spread_bps_median": float(group["half_spread_bps"].median()),
            "half_spread_bps_p90": float(group["half_spread_bps"].quantile(0.9)),
        }
        for size in BOOK_SIZES:
            for side in ("buy", "sell"):
                column = f"{side}_{size}"
                if column in group and group[column].notna().any():
                    row[f"{column}_bps_median"] = float(group[column].median())
        out.append(row)
    return out
