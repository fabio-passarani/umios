"""Everything the Freqtrade strategy adapter needs, with no Freqtrade import.

Freqtrade stays the *execution framework* (exchange connectivity, order
lifecycle, dry-run wallet, its own UI/API), while the trading rules, the sizing
and — above all — the risk veto come from this code base, so there is exactly
one implementation of each and Freqtrade cannot place an entry the risk engine
has not approved.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from qts.core.config import AppConfig, Settings, load_config
from qts.core.errors import LiveTradingDisabledError
from qts.core.logging import configure_logging, get_logger
from qts.core.types import Order, OrderReason, Side, TradingMode
from qts.portfolio.sizing import PositionSizer
from qts.risk.engine import RiskContext, RiskEngine
from qts.risk.kill_switch import KillSwitch
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
    ) -> None:
        self.config = config
        self.mode = mode
        self.strategy: Strategy = build_strategy(config.strategy)
        self.sizer = PositionSizer(config.portfolio.sizing)
        self.risk = RiskEngine(config.risk, mode, kill_switch=kill_switch)
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
        log.info("bridge_ready", mode=mode.value, fingerprint=config.fingerprint())
        return cls(config, mode, KillSwitch(kill_path), state_path)

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
