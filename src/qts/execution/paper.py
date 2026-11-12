"""Paper trading: the validated strategy running forward on new candles with a
simulated account. No exchange credentials are used and no real order exists.

Each :meth:`PaperTrader.step` call:

1. refreshes market data (optional) and checks it is fresh;
2. restores the session state saved by the previous step;
3. processes every candle that closed since then through the *same*
   :class:`~qts.execution.session.TradingSession` used by backtests;
4. persists equity, fills, trades, positions, risk decisions and state.

Daily strategy, daily candles: a decision taken at the close of day *D* is
filled at the open of *D+1* and booked when the *D+1* candle is complete. For
tick-by-tick simulation against the live order book use the Freqtrade dry-run
service (see docs/freqtrade.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from qts.backtesting.benchmark import buy_and_hold
from qts.backtesting.engine import build_session, compute_signal_frames, iter_bars
from qts.backtesting.metrics import compute_metrics
from qts.core.config import AppConfig
from qts.core.errors import ConfigError, DataError, QtsError
from qts.core.logging import get_logger
from qts.core.types import OrderReason, Trade, TradingMode
from qts.execution.session import TradingSession
from qts.integrations.db.storage import Storage
from qts.market_data.schema import TIMEFRAME_TO_FREQ
from qts.market_data.service import load_universe, update_history
from qts.market_data.sources.base import MarketDataSource
from qts.market_data.store import ParquetStore
from qts.risk.journal import RiskJournal
from qts.risk.kill_switch import KillSwitch
from qts.strategies import build_strategy

log = get_logger("paper")
SERVICE = "paper-trader"


@dataclass(frozen=True)
class PaperStepReport:
    run_id: str
    bars_processed: int
    fills: int
    rejected_orders: int
    equity: float
    open_positions: int
    kill_switch_active: bool
    last_bar: datetime | None


class PaperTrader:
    """Stateful forward simulation backed by the operational database."""

    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        store: ParquetStore,
        kill_switch: KillSwitch,
        source: MarketDataSource | None = None,
    ) -> None:
        if config.mode is not TradingMode.PAPER:
            raise ConfigError("paper trading requires mode: paper in the configuration")
        self.config = config
        self.storage = storage
        self.store = store
        self.kill_switch = kill_switch
        self.source = source
        self.strategy = build_strategy(config.strategy)

    # ------------------------------------------------------------------ session
    def start(self, start_from: str | None = None) -> str:
        """Open a new paper session. By default it trades only candles that close
        *after* now; ``start_from`` replays history from that date instead
        (useful for a dry run of the whole stack — clearly not live results)."""
        if self.storage.active_paper_run() is not None:
            raise ConfigError("a paper session is already running: stop it first")
        data = load_universe(self.config, self.store)
        last_bar = max(frame.index[-1] for frame in data.values())
        if start_from is not None:
            cutoff = pd.Timestamp(start_from, tz="UTC") - TIMEFRAME_TO_FREQ["1d"]
        else:
            cutoff = last_bar
        run_id = self.storage.create_run(
            kind="paper",
            mode=TradingMode.PAPER.value,
            strategy=self.strategy.name,
            config=self.config.model_dump(mode="json"),
            fingerprint=self.config.fingerprint(),
            initial_capital=self.config.portfolio.initial_capital,
        )
        session = build_session(self.config, self.strategy, TradingMode.PAPER, self.kill_switch)
        state = session.snapshot()
        state["last_bar_time"] = cutoff.to_pydatetime().isoformat()
        state["peak_equity"] = self.config.portfolio.initial_capital
        self.storage.save_paper_state(run_id, state)
        self.storage.record_event("info", SERVICE, f"paper session {run_id} started")
        return run_id

    def stop(self) -> str | None:
        """Close the active session (state is kept for inspection)."""
        run = self.storage.active_paper_run()
        if run is None:
            return None
        self.storage.finish_run(run.id, "stopped")
        self.storage.record_event("info", SERVICE, f"paper session {run.id} stopped")
        return run.id

    def request_exit(
        self,
        symbols: list[str] | None = None,
        reason: OrderReason = OrderReason.MANUAL,
        fraction: float = 1.0,
    ) -> list[str]:
        """Operator action: close (or partly close) open positions at the next open.

        ``symbols=None`` means every open position (emergency flatten). The
        request is stored in the session state and executed by the next
        :meth:`step` through the risk engine's reduce-only path, so it works
        with the kill switch active and is journaled like any other order.
        Returns the symbols for which an exit was queued.
        """
        run = self.storage.active_paper_run()
        if run is None:
            raise ConfigError("no active paper session")
        state = self.storage.load_paper_state(run.id)
        if state is None:
            raise ConfigError(f"paper session {run.id} has no saved state")
        session = build_session(self.config, self.strategy, TradingMode.PAPER, self.kill_switch)
        session.restore(state)
        targets = symbols if symbols is not None else sorted(session.portfolio.positions)
        queued = [s for s in targets if session.request_exit(s, reason, fraction)]
        if queued:
            # Only the pending decisions change: equity peak and everything else stay as saved.
            state["pending"] = session.snapshot()["pending"]
            self.storage.save_paper_state(run.id, state)
            self.storage.record_event(
                "warning",
                SERVICE,
                f"{reason.value} exit requested for {', '.join(queued)} (fraction {fraction:g})",
            )
        return queued

    # --------------------------------------------------------------------- step
    def step(self, now: datetime | None = None) -> PaperStepReport:
        """Process all candles closed since the previous step."""
        now = now or datetime.now(tz=UTC)
        run = self.storage.active_paper_run()
        if run is None:
            raise ConfigError("no active paper session: run `qts paper start` first")
        if run.config_fingerprint != self.config.fingerprint():
            raise ConfigError(
                "configuration changed since this paper session started "
                f"({run.config_fingerprint} -> {self.config.fingerprint()}): "
                "stop it and start a new one so results stay attributable to one config"
            )
        state = self.storage.load_paper_state(run.id)
        if state is None:
            raise ConfigError(f"paper session {run.id} has no saved state")

        journal = RiskJournal()
        session = build_session(
            self.config, self.strategy, TradingMode.PAPER, self.kill_switch, journal
        )
        session.restore(state)
        peak_before = float(state.get("peak_equity", self.config.portfolio.initial_capital))

        try:
            if self.source is not None:
                update_history(self.config, self.source, self.store)
            data = load_universe(self.config, self.store)
            self._check_freshness(data, now)
        except QtsError as exc:
            session.risk.record_error(now, str(exc))
            self._persist(run.id, session, journal, peak_before, None)
            self.storage.record_event("error", SERVICE, f"data problem: {exc}")
            self.storage.heartbeat(SERVICE, "degraded", str(exc))
            raise
        session.risk.record_success()

        last_done = session.last_bar_time
        signals = compute_signal_frames(self.strategy, data)
        start = pd.Timestamp(last_done) + pd.Timedelta(seconds=1) if last_done else None
        timeline = iter_bars(data, signals, start, None)
        if not session.last_close and last_done is not None:
            # First step: trusted reference prices come from the last bar before the session.
            for symbol, frame in data.items():
                history = frame.loc[frame.index <= pd.Timestamp(last_done)]
                if not history.empty:
                    session.last_close[symbol] = float(history["close"].iloc[-1])
        try:
            for timestamp, bars, rows in timeline:
                session.process_bar(timestamp.to_pydatetime(), bars, rows)
        except QtsError as exc:
            # The broker (or anything on the order path) failed in the middle of a
            # bar. The in-memory session is half-advanced and must not be saved:
            # keep the last committed state, count the failure, and let the next
            # step redo these candles from scratch.
            self._register_failure(run.id, state, now, f"execution problem: {exc}")
            raise

        benchmark = None
        if session.equity_curve:
            first_ever = self.storage.equity_curve(run.id)
            origin = first_ever.index[0] if len(first_ever) else session.equity_curve[0].timestamp
            benchmark = buy_and_hold(
                data,
                self.config.portfolio.initial_capital,
                self.config.costs,
                pd.Timestamp(origin),
                None,
            )
        self._persist(run.id, session, journal, peak_before, benchmark)
        self.storage.heartbeat(
            SERVICE, "halted" if self.kill_switch.active else "ok", f"{len(timeline)} bars"
        )
        report = PaperStepReport(
            run_id=run.id,
            bars_processed=len(timeline),
            fills=len(session.portfolio.fills),
            rejected_orders=sum(1 for d in journal.decisions if not d.approved),
            equity=session.portfolio.equity(session.last_close),
            open_positions=len(session.portfolio.positions),
            kill_switch_active=self.kill_switch.active,
            last_bar=session.last_bar_time,
        )
        log.info("paper_step", **{k: str(v) for k, v in report.__dict__.items()})
        return report

    # ------------------------------------------------------------------ helpers
    def _register_failure(
        self, run_id: str, state: dict[str, Any], now: datetime, detail: str
    ) -> None:
        """Count an operational failure against the saved state without advancing it."""
        journal = RiskJournal()
        clean = build_session(
            self.config, self.strategy, TradingMode.PAPER, self.kill_switch, journal
        )
        clean.restore(state)
        clean.risk.record_error(now, detail)
        kept = dict(state)
        kept["risk"] = clean.risk.snapshot()
        with self.storage.transaction():
            self.storage.save_paper_state(run_id, kept)
            self.storage.add_risk_events(run_id, journal.events)
        self.storage.record_event("error", SERVICE, detail)
        self.storage.heartbeat(SERVICE, "degraded", detail)

    def _check_freshness(self, data: dict[str, pd.DataFrame], now: datetime) -> None:
        limit = pd.Timedelta(hours=self.config.risk.max_data_age_hours)
        bar = TIMEFRAME_TO_FREQ[self.config.market.timeframe]
        for symbol, frame in data.items():
            age = pd.Timestamp(now) - (frame.index[-1] + bar)
            if age > limit:
                raise DataError(
                    f"{symbol}: newest candle closed {age} ago (limit {limit}): data is stale"
                )

    def _persist(
        self,
        run_id: str,
        session: TradingSession,
        journal: RiskJournal,
        peak_before: float,
        benchmark: pd.Series | None,
    ) -> None:
        # One transaction for the whole step: the fills, the trades and the resume
        # point (paper state) are committed together or not at all. Written
        # separately, a crash in between would leave the fills stored but the
        # state not advanced, and the retry would book the same orders twice.
        with self.storage.transaction():
            self._write_step(run_id, session, journal, peak_before, benchmark)

    def _write_step(
        self,
        run_id: str,
        session: TradingSession,
        journal: RiskJournal,
        peak_before: float,
        benchmark: pd.Series | None,
    ) -> None:
        curve = session.equity_curve
        if curve:
            index = pd.DatetimeIndex([p.timestamp for p in curve])
            self.storage.append_equity(
                run_id,
                pd.Series([p.equity for p in curve], index=index),
                pd.Series([p.cash for p in curve], index=index),
                pd.Series([p.exposure for p in curve], index=index),
                peak_before,
                benchmark,
            )
        self.storage.add_fills(run_id, session.portfolio.fills)
        self.storage.add_trades(run_id, session.portfolio.trades)
        self.storage.add_risk_decisions(run_id, journal.decisions)
        self.storage.add_risk_events(run_id, journal.events)
        self.storage.add_signals(run_id, session.signal_log)
        self.storage.replace_positions(
            run_id, session.portfolio.positions.values(), session.last_close
        )
        state = session.snapshot()
        state["peak_equity"] = max([peak_before, *(p.equity for p in curve)])
        self.storage.save_paper_state(run_id, state)
        self._refresh_metrics(run_id)

    def _refresh_metrics(self, run_id: str) -> None:
        curve = self.storage.equity_curve(run_id)
        if curve.empty:
            return
        trades = [
            Trade(
                symbol=t.symbol,
                quantity=t.quantity,
                entry_time=t.entry_time,
                exit_time=t.exit_time,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                fees=t.fees,
                pnl=t.pnl,
                return_pct=t.return_pct,
                exit_reason=OrderReason(t.exit_reason),
            )
            for t in self.storage.trades(run_id)
        ]
        metrics = compute_metrics(
            curve["equity"], self.config.portfolio.initial_capital, trades, curve["exposure"]
        )
        self.storage.update_metrics(run_id, metrics)
