"""Freqtrade adapter for the platform's trend-following strategy.

This file is intentionally thin. It contains **no trading logic**:

* signals come from ``qts.strategies`` (same code as the platform backtester);
* position size comes from ``qts.portfolio.sizing``;
* every entry must be approved by ``qts.risk`` in ``confirm_trade_entry`` —
  Freqtrade cannot open a position the independent risk engine rejects;
* parameters live in the platform YAML config (``QTS_CONFIG_FILE``).

Licensing note: this module imports Freqtrade (GPLv3) and is therefore the only
part of the repository that is a derivative work of it. See docs/licensing.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute
from pandas import DataFrame

from qts.integrations.freqtrade import FreqtradeBridge


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
        equity, _cash = self._equity_and_cash()
        if equity > 0:
            self.bridge.on_equity(current_time, equity)

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

    # --------------------------------------------------------------------- stop
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
        entry_candle = self._signal_candle(pair, trade.open_date_utc)
        if entry_candle is None:
            return None
        stop_price = trade.open_rate - float(entry_candle["qts_stop_distance"])
        if self._trailing:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            held = dataframe.loc[
                (dataframe["date"] >= entry_candle["date"]) & (dataframe["date"] < current_time)
            ]
            if not held.empty:
                trail = float(held["close"].max() - held["qts_trail_distance"].iloc[-1])
                stop_price = max(stop_price, trail)
        if stop_price <= 0:
            return None
        return stoploss_from_absolute(stop_price, current_rate, is_short=False, leverage=1.0)
