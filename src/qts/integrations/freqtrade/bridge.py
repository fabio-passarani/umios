"""Everything the Freqtrade strategy adapter needs, with no Freqtrade import.

Freqtrade stays the *execution framework* (exchange connectivity, order
lifecycle, dry-run wallet, its own UI/API), while the trading rules, the sizing
and — above all — the risk veto come from this code base, so there is exactly
one implementation of each.

Authorisation model (the same as the platform's own session):

* **New exposure** — :meth:`FreqtradeBridge.approve_entry`. Fails *closed*:
  any rejection, doubt or internal error means "do not enter".
* **Reductions** — :meth:`FreqtradeBridge.authorize_exit`. Every stop, signal
  exit, take profit, partial, manual or emergency close is evaluated by the risk
  engine's reduce-only path and journaled. It fails *open*: a kill switch, a
  circuit breaker, a database outage or a bug in this module must never keep
  the bot inside a position.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from qts.core.config import AppConfig, Settings, load_config
from qts.core.errors import ConfigError, LiveTradingDisabledError, QtsError
from qts.core.logging import configure_logging, get_logger
from qts.core.types import Order, OrderReason, Side, TradingMode
from qts.portfolio.sizing import PositionSizer
from qts.risk.engine import RiskContext, RiskEngine
from qts.risk.journal import RiskEvent, RiskJournal
from qts.risk.kill_switch import KillSwitch
from qts.risk.models import RiskDecision
from qts.strategies import Strategy, build_strategy

log = get_logger("freqtrade.bridge")

#: Freqtrade run modes mapped to platform modes. Anything not listed (notably
#: "live") maps to LIVE, which the risk engine rejects unconditionally.
_RUNMODES = {
    "backtest": TradingMode.BACKTEST,
    "hyperopt": TradingMode.BACKTEST,
    "plot": TradingMode.BACKTEST,
    "util_exchange": TradingMode.BACKTEST,
    "util_no_exchange": TradingMode.BACKTEST,
    "other": TradingMode.BACKTEST,
    "dry_run": TradingMode.PAPER,
}


#: Freqtrade exit reasons -> platform order reasons. Unknown reasons are treated
#: as manual interventions: still a reduction, still authorised, still journaled.
_EXIT_REASONS = {
    "exit_signal": OrderReason.EXIT_SIGNAL,
    "partial_exit": OrderReason.EXIT_SIGNAL,
    "stop_loss": OrderReason.STOP_LOSS,
    "stoploss": OrderReason.STOP_LOSS,
    "stoploss_on_exchange": OrderReason.STOP_LOSS,
    "trailing_stop_loss": OrderReason.TRAILING_STOP,
    "roi": OrderReason.TAKE_PROFIT,
    "take_profit": OrderReason.TAKE_PROFIT,
    "force_exit": OrderReason.MANUAL,
    "emergency_exit": OrderReason.EMERGENCY,
    "liquidation": OrderReason.EMERGENCY,
    "kill_switch": OrderReason.KILL_SWITCH,
}
SERVICE = "freqtrade"


def exit_reason(freqtrade_reason: str | None) -> OrderReason:
    """Map a Freqtrade exit reason (or custom exit tag) to an :class:`OrderReason`."""
    return _EXIT_REASONS.get((freqtrade_reason or "").lower(), OrderReason.MANUAL)


def resolve_mode(runmode: str, dry_run: bool) -> TradingMode:
    """Translate Freqtrade's run mode; real-money trading resolves to LIVE."""
    mode = _RUNMODES.get(runmode.lower(), TradingMode.LIVE)
    if mode is TradingMode.PAPER and not dry_run:
        return TradingMode.LIVE
    return mode


class FreqtradeBridge:
    """Holds the platform strategy, sizer and risk engine for one Freqtrade bot."""

    def __init__(
        self,
        config: AppConfig,
        mode: TradingMode,
        kill_switch: KillSwitch | None = None,
        state_path: Path | None = None,
        decision_sink: Callable[[RiskDecision], None] | None = None,
        event_sink: Callable[[RiskEvent], None] | None = None,
        heartbeat: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.mode = mode
        self.strategy: Strategy = build_strategy(config.strategy)
        self.sizer = PositionSizer(config.portfolio.sizing)
        self._decision_sink = decision_sink
        self._event_sink = event_sink
        self._heartbeat = heartbeat
        self._last_beat: datetime | None = None
        journal = RiskJournal(sinks=[self._record_decision], event_sinks=[self._record_event])
        self.risk = RiskEngine(config.risk, mode, kill_switch=kill_switch, journal=journal)
        if config.market.timeframe != "1d":
            # The adapter strategy declares daily candles to Freqtrade and reasons
            # in whole days; running it on another timeframe would silently
            # mis-align signals and stops.
            raise ConfigError(
                f"the Freqtrade adapter supports daily candles only, not {config.market.timeframe}"
            )
        self._bar = timedelta(days=1)
        self._state_path = state_path
        self._saved: str | None = None
        self._restore_risk_state()

    # ------------------------------------------------------------ risk state
    def _restore_risk_state(self) -> None:
        """Reload the circuit-breaker state saved by the previous bot process.

        Without it a restart would forget the equity peak, the daily loss halt
        and the day's order count — i.e. restarting the bot would be a way to
        reset the drawdown limit. An unreadable file is an anomaly: fail closed.
        """
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise TypeError("risk state must be a mapping")
            self.risk.restore(state)
            self._saved = json.dumps(state, sort_keys=True)
        except (OSError, ValueError, TypeError) as exc:
            log.error("risk_state_unreadable", error=type(exc).__name__)
            self.risk.kill_switch.activate(
                "freqtrade risk state file is unreadable", source="system"
            )

    def _save_risk_state(self) -> None:
        if self._state_path is None:
            return
        payload = json.dumps(self.risk.snapshot(), sort_keys=True)
        if payload == self._saved:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._state_path)
        self._saved = payload

    @classmethod
    def from_environment(cls, runmode: str, dry_run: bool) -> FreqtradeBridge:
        """Build from ``QTS_CONFIG_FILE`` / ``QTS_STATE_DIR`` (same files as the CLI)."""
        settings = Settings()
        configure_logging(settings.log_level, settings.log_json)
        config = load_config(os.environ.get("QTS_CONFIG_FILE", str(settings.config_file)))
        mode = resolve_mode(runmode, dry_run)
        if mode is TradingMode.LIVE:
            # Refuse to start at all. The risk engine would veto every entry in
            # LIVE mode anyway, but a bot that is up and connected with real keys
            # could still manage exits; there must be no such bot.
            raise LiveTradingDisabledError(
                f"Freqtrade was started for real trading (runmode={runmode!r}, "
                f"dry_run={dry_run}): live trading is not implemented in this version. "
                "Use `freqtrade trade --dry-run` or backtesting."
            )
        # Backtests must not read or trip the operator's persistent kill switch,
        # nor inherit the live circuit-breaker state.
        paper = mode is TradingMode.PAPER
        kill_path: Path | None = settings.state_dir / "kill_switch.json" if paper else None
        state_path: Path | None = (
            settings.state_dir / "freqtrade_risk_state.json" if paper else None
        )
        decision_sink, event_sink, heartbeat = (
            _database_sinks(settings, config) if paper else (None, None, None)
        )
        log.info(
            "bridge_ready",
            mode=mode.value,
            fingerprint=config.fingerprint(),
            journal="database" if decision_sink else "log",
        )
        return cls(
            config, mode, KillSwitch(kill_path), state_path, decision_sink, event_sink, heartbeat
        )

    # ------------------------------------------------------------- recording
    def _record_decision(self, decision: RiskDecision) -> None:
        """Forward a decision to the database. Never raises: the log line written
        by the journal is the fallback record, and a database outage must not
        turn into a blocked exit."""
        if self._decision_sink is None:
            return
        try:
            self._decision_sink(decision)
        except Exception as exc:
            log.error("risk_journal_write_failed", error=type(exc).__name__)

    def _record_event(self, event: RiskEvent) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event)
        except Exception as exc:
            log.error("risk_event_write_failed", error=type(exc).__name__)

    def beat(self, now: datetime) -> None:
        """Service heartbeat for the monitoring stack (at most once a minute)."""
        if self._heartbeat is None:
            return
        if self._last_beat is not None and (now - self._last_beat) < timedelta(seconds=55):
            return
        self._last_beat = now
        try:
            status = "halted" if self.risk.kill_switch.active else "ok"
            self._heartbeat(status, f"mode={self.mode.value}")
        except Exception as exc:
            log.error("heartbeat_failed", error=type(exc).__name__)

    @property
    def startup_candles(self) -> int:
        return self.strategy.warmup_bars

    def signals(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        """Compute platform signals for a Freqtrade OHLCV frame (``date`` column).

        Returns a frame aligned row-by-row with the input, columns prefixed ``qts_``.
        """
        candles = dataframe[["open", "high", "low", "close", "volume"]].copy()
        candles.index = pd.DatetimeIndex(dataframe["date"])
        result = self.strategy.generate_signals(candles)
        result.index = dataframe.index
        return result.add_prefix("qts_")

    def stake_amount(self, equity: float, cash: float, price: float, stop_distance: float) -> float:
        """Stake (in quote currency) for a new entry; 0.0 means 'do not enter'."""
        quantity = self.sizer.size(
            equity=equity,
            cash=cash,
            price=price,
            stop_distance=stop_distance,
            cost_rate=self.config.costs.cost_rate,
        )
        return quantity * price

    def on_equity(self, now: datetime, equity: float) -> None:
        """Feed account equity to the circuit breakers (daily loss, drawdown)."""
        self.risk.on_equity_update(now, equity)
        self._save_risk_state()

    def report_error(self, now: datetime, detail: str) -> None:
        """An exchange/data/internal failure seen by the adapter. Repeated
        failures trip the kill switch (``max_consecutive_errors``)."""
        self.risk.record_error(now, detail)
        self._save_risk_state()

    def report_healthy(self) -> None:
        """A complete, successful bot loop: resets the consecutive-error count."""
        self.risk.record_success()

    @property
    def flatten_requested(self) -> bool:
        """True when the kill switch is active and configured to close positions."""
        return self.config.risk.flatten_on_kill_switch and self.risk.kill_switch.active

    def approve_entry(
        self,
        pair: str,
        amount: float,
        rate: float,
        stop_distance: float,
        now: datetime,
        cash: float,
        positions: dict[str, float],
        prices: dict[str, float],
        last_candle_open: datetime,
    ) -> bool:
        """Ask the risk engine whether Freqtrade may open this position."""
        order = Order(
            symbol=pair,
            side=Side.BUY,
            quantity=amount,
            reference_price=rate,
            reason=OrderReason.ENTRY,
            strategy=self.strategy.name,
            created_at=now,
            stop_price=rate - stop_distance if stop_distance == stop_distance else None,
        )
        context = RiskContext(
            now=now,
            cash=cash,
            positions=positions,
            prices=prices,
            data_time=min(last_candle_open + self._bar, now),
            cost_rate=self.config.costs.cost_rate,
        )
        approved = self.risk.evaluate(order, context).approved
        self._save_risk_state()
        return approved

    def authorize_exit(
        self,
        pair: str,
        amount: float,
        rate: float,
        reason: str | None,
        now: datetime,
        held: float,
    ) -> bool:
        """Authorise and journal a reduction of ``held`` by ``amount``.

        Returns ``False`` only when the order is *not* a reduction (it would
        sell more than is held). Anything else — including an internal error
        while evaluating — returns ``True``: see the module docstring.
        """
        try:
            order = Order(
                symbol=pair,
                side=Side.SELL,
                quantity=amount,
                reference_price=rate,
                reason=exit_reason(reason),
                strategy=self.strategy.name,
                created_at=now,
            )
            context = RiskContext(
                now=now, cash=0.0, positions={pair: held}, prices={pair: rate}, data_time=now
            )
            decision = self.risk.evaluate(order, context)
        except Exception as exc:
            log.error("exit_authorization_failed_open", pair=pair, error=type(exc).__name__)
            return True
        if decision.approved:
            return True
        oversell = any(c.name == "no_short_selling" for c in decision.failed_checks)
        if not oversell:
            # e.g. a rounding artefact in the rate: not a reason to stay in the market
            log.error("exit_allowed_despite_rejection", pair=pair, detail=decision.reason)
        return not oversell


def _database_sinks(
    settings: Settings, config: AppConfig
) -> tuple[
    Callable[[RiskDecision], None] | None,
    Callable[[RiskEvent], None] | None,
    Callable[[str, str], None] | None,
]:
    """Journal the bot's risk decisions in the platform database, when one is
    configured (``QTS_DATABASE_URL``), so they appear on the same dashboard as
    the paper trader's. Without it — or if it is unreachable at start-up — the
    structured log remains the journal and the bot still runs."""
    if "QTS_DATABASE_URL" not in os.environ and settings.db_host is None:
        return None, None, None
    try:
        from qts.integrations.db.storage import Storage  # local: optional dependency path

        storage = Storage(settings.resolved_database_url())
        storage.init_schema()
        run = storage.active_run(SERVICE)
        fingerprint = config.fingerprint()
        if run is not None and run.config_fingerprint != fingerprint:
            storage.finish_run(run.id, "stopped")
            run = None
        run_id = (
            run.id
            if run is not None
            else storage.create_run(
                kind=SERVICE,
                mode=TradingMode.PAPER.value,
                strategy=config.strategy.name,
                config=config.model_dump(mode="json"),
                fingerprint=fingerprint,
                initial_capital=config.portfolio.initial_capital,
            )
        )
    except QtsError as exc:
        log.error("risk_journal_database_unavailable", error=type(exc).__name__)
        return None, None, None

    def decisions(decision: RiskDecision) -> None:
        storage.add_risk_decisions(run_id, [decision])

    def events(event: RiskEvent) -> None:
        storage.add_risk_events(run_id, [event])

    def heartbeat(status: str, detail: str) -> None:
        storage.heartbeat(SERVICE, status, detail)

    storage.record_event("info", SERVICE, f"freqtrade dry-run attached to run {run_id}")
    return decisions, events, heartbeat
