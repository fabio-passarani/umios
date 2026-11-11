"""Store a finished backtest in the operational database (for the dashboard)."""

from __future__ import annotations

import pandas as pd

from qts.backtesting.engine import BacktestResult
from qts.integrations.db.storage import Storage


def save_backtest(
    storage: Storage,
    result: BacktestResult,
    metrics: dict[str, float],
    benchmark: pd.Series | None = None,
) -> str:
    """Persist a backtest run with everything needed to chart and audit it."""
    run_id = storage.create_run(
        kind="backtest",
        mode="backtest",
        strategy=str(result.metadata.get("strategy", "unknown")),
        config=result.config,
        fingerprint=result.config_fingerprint,
        initial_capital=result.initial_capital,
    )
    storage.append_equity(
        run_id, result.equity, result.cash, result.exposure, result.initial_capital, benchmark
    )
    storage.add_fills(run_id, result.fills)
    storage.add_trades(run_id, result.trades)
    storage.add_risk_decisions(run_id, result.decisions)
    storage.add_risk_events(run_id, result.risk_events)
    last_prices = {p.symbol: p.highest_close or p.avg_price for p in result.open_positions}
    storage.replace_positions(run_id, result.open_positions, last_prices)
    storage.finish_run(run_id, "completed", metrics)
    return run_id
