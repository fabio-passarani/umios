"""Freqtrade adapter for the platform's trend-following strategy.

This file is intentionally thin. It contains **no trading logic**:

* signals come from ``qts.strategies`` (same code as the platform backtester);
* position size comes from ``qts.portfolio.sizing``;
* parameters live in the platform YAML config (``QTS_CONFIG_FILE``);
* every order goes through ``qts.risk``:

  - **entries** in ``confirm_trade_entry`` — fail closed: rejected, in doubt or
    on any internal error, Freqtrade does not enter;
  - **exits** (stop, signal, take profit, partial, manual, emergency) in
    ``confirm_trade_exit`` — reduce-only path, journaled, fail open: nothing in
    this adapter may keep the bot inside a position.

The protective stop is computed once, when the entry fills, and stored with the
trade in Freqtrade's database (``custom_data``). After a restart the stop is read
back from there, not recomputed from candles that may no longer be in memory.

Licensing note: this module imports Freqtrade (GPLv3) and is therefore the only
part of the repository that is a derivative work of it. See docs/licensing.md.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute
from pandas import DataFrame

from qts.integrations.freqtrade import FreqtradeBridge

STOP_KEY = "qts_stop_price"  # initial protective stop, fixed at entry
TRAIL_KEY = "qts_trail_stop"  # highest trailing stop reached so far (only ratchets up)


class QtsTrendFollowing(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = False
    process_only_new_candles = True

    # Exits are driven by the strategy signal and the ATR stop, not by ROI targets.
    minimal_roi = {"0": 100.0}  # noqa: RUF012 - Freqtrade reads plain class attributes
    stoploss = -0.99  # safety net only; the real stop is custom_stoploss below
    use_custom_stoploss = True
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    position_adjustment_enable = False

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        runmode = getattr(config.get("runmode"), "value", str(config.get("runmode", "other")))
        self.bridge = FreqtradeBridge.from_environment(runmode, bool(config.get("dry_run", True)))
        self.startup_candle_count = self.bridge.startup_candles
        self._trailing = self.bridge.config.strategy.params.trailing_stop

    # ------------------------------------------------------------------ signals
    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        signals = self.bridge.signals(dataframe)
        for column in signals.columns:
            dataframe[column] = signals[column]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        dataframe.loc[dataframe["qts_entry"] & (dataframe["volume"] > 0), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, Any]) -> DataFrame:
        dataframe.loc[dataframe["qts_exit"], "exit_long"] = 1
        return dataframe

    # ------------------------------------------------------------------ helpers
    def _signal_candle(self, pair: str, before: datetime) -> Any | None:
        """Last fully closed candle strictly before ``before`` (never the forming one)."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return None
        closed = dataframe.loc[
            dataframe["date"] < before.replace(hour=0, minute=0, second=0, microsecond=0)
        ]
        return None if closed.empty else closed.iloc[-1]

    def _equity_and_cash(self) -> tuple[float, float]:
        currency = self.config["stake_currency"]
        return (
            float(self.wallets.get_total_stake_amount()) if self.wallets else 0.0,
            float(self.wallets.get_free(currency)) if self.wallets else 0.0,
        )

    # ------------------------------------------------------------------- sizing
    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        candle = self._signal_candle(pair, current_time)
        if candle is None:
            return 0.0
        equity, cash = self._equity_and_cash()
        stake = self.bridge.stake_amount(
            equity, cash, current_rate, float(candle["qts_stop_distance"])
        )
        stake = min(stake, max_stake)
        if min_stake is not None and stake < min_stake:
            return 0.0  # too small to trade: skip rather than oversize
        return stake

    # --------------------------------------------------------------------- risk
    def bot_loop_start(self, current_time: datetime, **kwargs: Any) -> None:
        """Feed equity to the circuit breakers once per loop. A failure here is an
        operational error (exchange or wallet unavailable): it is counted, and
        repeated failures trip the kill switch; it never crashes the bot."""
        try:
            equity, _cash = self._equity_and_cash()
            if equity > 0:
                self.bridge.on_equity(current_time, equity)
            self.bridge.report_healthy()
        except Exception as exc:
            self.bridge.report_error(current_time, f"bot loop: {type(exc).__name__}")
        self.bridge.beat(current_time)

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        """New exposure: fail closed. Any exception means *no entry*."""
        try:
            return self._approve_entry(pair, amount, rate, current_time, side)
        except Exception as exc:
            self.bridge.report_error(current_time, f"entry check {pair}: {type(exc).__name__}")
            return False

    def _approve_entry(
        self, pair: str, amount: float, rate: float, current_time: datetime, side: str
    ) -> bool:
        if side != "long":
            return False
        candle = self._signal_candle(pair, current_time)
        if candle is None:
            return False
        equity, cash = self._equity_and_cash()
        self.bridge.on_equity(current_time, equity)
        positions: dict[str, float] = {}
        prices: dict[str, float] = {pair: float(candle["close"])}
        for trade in Trade.get_trades_proxy(is_open=True):
            positions[trade.pair] = positions.get(trade.pair, 0.0) + float(trade.amount)
            other = self._signal_candle(trade.pair, current_time)
            prices[trade.pair] = float(other["close"]) if other is not None else trade.open_rate
        return self.bridge.approve_entry(
            pair=pair,
            amount=amount,
            rate=rate,
            stop_distance=float(candle["qts_stop_distance"]),
            now=current_time,
            cash=cash,
            positions=positions,
            prices=prices,
            last_candle_open=candle["date"].to_pydatetime(),
        )

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs: Any,
    ) -> bool:
        """Reductions: authorised and journaled by the risk engine, fail open."""
        try:
            return self.bridge.authorize_exit(
                pair=pair,
                amount=float(amount),
                rate=float(rate),
                reason=exit_reason,
                now=current_time,
                held=float(trade.amount),
            )
        except Exception:
            return True

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | bool | None:
        """Emergency close: with ``flatten_on_kill_switch`` an active kill switch
        closes every open position (the reason is journaled as ``kill_switch``)."""
        return "kill_switch" if self.bridge.flatten_requested else None

    # --------------------------------------------------------------------- stop
    def order_filled(
        self, pair: str, trade: Trade, order: Any, current_time: datetime, **kwargs: Any
    ) -> None:
        """Fix the protective stop the moment the entry fills and store it with
        the trade, so it survives restarts and candle-history truncation."""
        if getattr(order, "ft_order_side", None) != trade.entry_side:
            self._journal_partial_exit(pair, trade, order, current_time)
            return
        if _number(trade.get_custom_data(STOP_KEY)) is not None:
            return
        candle = self._signal_candle(pair, trade.open_date_utc)
        if candle is None:
            self.bridge.report_error(current_time, f"no signal candle to set the stop of {pair}")
            return
        stop = float(trade.open_rate) - float(candle["qts_stop_distance"])
        if math.isfinite(stop) and stop > 0:
            trade.set_custom_data(STOP_KEY, stop)

    def _journal_partial_exit(
        self, pair: str, trade: Trade, order: Any, current_time: datetime
    ) -> None:
        """Freqtrade does not call ``confirm_trade_exit`` for partial exits, so a
        partial would leave no trace in the risk journal. A fill that leaves the
        trade open is a partial: record it here (full exits were recorded when
        they were confirmed). Recording only — the order has already executed."""
        if not getattr(trade, "is_open", False):
            return
        try:
            amount = float(order.safe_filled)
            rate = float(order.safe_price)
            self.bridge.authorize_exit(
                pair=pair,
                amount=amount,
                rate=rate,
                reason="partial_exit",
                now=current_time,
                held=float(trade.amount) + amount,
            )
        except Exception as exc:
            self.bridge.report_error(current_time, f"partial exit journal: {type(exc).__name__}")

    def _initial_stop(self, pair: str, trade: Trade) -> float | None:
        stored = _number(trade.get_custom_data(STOP_KEY))
        if stored is not None:
            return stored
        # Trades opened before the stop was persisted (or a fill callback that
        # could not run): derive it once from the signal candle and store it.
        candle = self._signal_candle(pair, trade.open_date_utc)
        if candle is None:
            return None
        stop = float(trade.open_rate) - float(candle["qts_stop_distance"])
        if not (math.isfinite(stop) and stop > 0):
            return None
        trade.set_custom_data(STOP_KEY, stop)
        return stop

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float | None:
        stop_price = self._initial_stop(pair, trade)
        if stop_price is None:
            # Returning None keeps Freqtrade's current stop. Not knowing the stop
            # of an open position is an anomaly: count it towards the kill switch.
            self.bridge.report_error(current_time, f"stop of {pair} is unknown")
            return None
        if self._trailing:
            stop_price = max(stop_price, self._trail(pair, trade, current_time) or 0.0)
        return stoploss_from_absolute(stop_price, current_rate, is_short=False, leverage=1.0)

    def _trail(self, pair: str, trade: Trade, current_time: datetime) -> float | None:
        """Chandelier stop from closed candles since entry; only ever moves up."""
        best = _number(trade.get_custom_data(TRAIL_KEY))
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is not None and not dataframe.empty:
            day = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
            entry_day = trade.open_date_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            held = dataframe.loc[(dataframe["date"] >= entry_day) & (dataframe["date"] < day)]
            if not held.empty:
                candidate = float(held["close"].max() - held["qts_trail_distance"].iloc[-1])
                if math.isfinite(candidate) and (best is None or candidate > best):
                    best = candidate
                    trade.set_custom_data(TRAIL_KEY, best)
        return best


def _number(value: Any) -> float | None:
    """A finite positive float, or None (custom data comes back from the database)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None
