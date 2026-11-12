"""Assertions on what the stack recorded. Runs inside the platform container,
reading PostgreSQL with the application's own unprivileged account.

    check.py equals-backtest   the session equals an independent backtest of the delivered data
    check.py invariants        no duplicated order, every entry follows a signal and an approval
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qts.backtesting.engine import run_backtest
from qts.core.config import Settings, load_config
from qts.core.logging import configure_logging
from qts.core.types import TradingMode
from qts.integrations.db.storage import Storage
from qts.market_data.service import load_universe
from qts.market_data.store import ParquetStore


def fail(message: str) -> None:
    raise SystemExit(f"CHECK FAILED: {message}")


def main() -> None:
    configure_logging("CRITICAL", True)
    settings = Settings()
    config = load_config(settings.config_file)
    storage = Storage(settings.resolved_database_url())
    run = storage.active_paper_run()
    if run is None:
        fail("no active paper session")
    assert run is not None
    curve = storage.equity_curve(run.id)
    fills = storage.fills(run.id)
    decisions = storage.risk_decisions(run.id)
    signals = storage.signals(run.id)
    command = sys.argv[1]

    if command == "equals-backtest":
        data = load_universe(config, ParquetStore(config.data.directory))
        expected = run_backtest(
            config.model_copy(update={"mode": TradingMode.BACKTEST}), data, start=curve.index[0]
        )
        if len(curve) != len(expected.equity):
            fail(f"{len(curve)} candles recorded, the backtest has {len(expected.equity)}")
        if not np.allclose(
            curve["equity"].to_numpy(), expected.equity.to_numpy(), rtol=1e-12, atol=0
        ):
            worst = float(np.abs(curve["equity"].to_numpy() - expected.equity.to_numpy()).max())
            fail(f"equity differs from the backtest by up to {worst}")
        if len(fills) != len(expected.fills):
            fail(f"{len(fills)} fills recorded, the backtest has {len(expected.fills)}")
        recorded = [(f.symbol, f.side, f.reason, round(f.price, 6)) for f in fills]
        wanted = [
            (f.symbol, f.side.value, f.reason.value, round(f.price, 6)) for f in expected.fills
        ]
        if sorted(recorded) != sorted(wanted):
            fail("the fills are not the backtest's fills")
        print(
            f"equals-backtest OK: {len(curve)} candles, {len(fills)} fills, "
            f"final equity {curve['equity'].iloc[-1]:.6f} = {expected.equity.iloc[-1]:.6f}"
        )
    elif command == "invariants":
        ids = [f.order_id for f in fills]
        if len(ids) != len(set(ids)):
            fail("an order was booked twice")
        approved = {d.order_id for d in decisions if d.approved}
        if not {f.order_id for f in fills} <= approved:
            fail("a fill exists without a risk approval")
        if any(not d.approved for d in decisions if d.side == "sell"):
            fail("an exit was rejected")
        queued = {
            (pd.Timestamp(s.ts).tz_convert("UTC"), s.symbol)
            for s in signals
            if s.action == "entry_queued"
        }
        days = list(curve.index)
        for fill in (f for f in fills if f.side == "buy"):
            position = days.index(pd.Timestamp(fill.ts).tz_convert("UTC"))
            if (days[position - 1], fill.symbol) not in queued:
                fail(f"entry in {fill.symbol} on {fill.ts} was not preceded by a strategy signal")
        candle_days = {pd.Timestamp(s.ts).tz_convert("UTC") for s in signals}
        if set(days) - candle_days:
            fail("some processed candles have no recorded signal")
        if run.mode != "paper":
            fail(f"session mode is {run.mode}")
        buys = sum(1 for f in fills if f.side == "buy")
        print(
            f"invariants OK: {len(fills)} fills ({buys} entries, all following a strategy signal "
            f"and a risk approval), {len(signals)} signal rows over {len(days)} candles, no duplicates"
        )
    else:
        fail(f"unknown check {command!r}")


if __name__ == "__main__":
    main()
