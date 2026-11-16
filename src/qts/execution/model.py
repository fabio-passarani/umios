"""How an approved order turns into a fill.

Two models, one interface:

* :class:`LegacyExecution` — a fixed slippage and a fee on every fill, everything
  always fills. It is what every result published so far used, and it is kept
  bit-identical.
* :class:`RealisticExecution` — a fill depends on the market it meets: the
  spread is wider and the price impact larger in thin, volatile instruments;
  an order can only take a share of the volume that actually traded (the rest
  is a partial fill), may arrive late, may be refused by the venue.

What each number is — and it matters, because a cost model is only as good as
its weakest assumption:

=====================  =========  ====================================================
Component              Kind       Basis
=====================  =========  ====================================================
fee                    OBSERVED   the venue's published spot schedule (0.10% a side)
minimum order value    OBSERVED   the venue's order filter on the snapshot day
delayed fill price     OBSERVED   the open of the hourly candle the order reaches
traded volume          OBSERVED   the candle's own traded value
half spread            ESTIMATED  cross-section of live quotes on one day, as a
                                  function of traded value — *assumed* to hold in
                                  the past, which it need not
price impact           ESTIMATED  square-root law; coefficient from walking live
                                  order books on the same day
participation limit    ASSUMED    share of a candle's volume an order may take
rejection probability  ASSUMED    stress parameter, zero by default
delisting haircut      ASSUMED    loss on a position in an asset that stops trading
=====================  =========  ====================================================

There is no historical bid/ask in the data source, so no spread or slippage
figure produced here is a measurement. Results must be read as scenarios.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from qts.core.config import CostConfig, ExecutionConfig
from qts.core.types import Side


@dataclass(frozen=True, slots=True)
class MarketConditions:
    """What is known about an instrument when an order reaches the market.

    ``traded_value`` and ``volatility`` are trailing figures computed from candles
    *before* the execution candle (daily traded value in quote currency, daily
    volatility of returns). ``candle_value`` is what actually traded in the
    execution candle: the order cannot take more than a share of it.
    ``delayed_price`` is the price an order delayed by some hours would meet.
    """

    traded_value: float = math.nan
    volatility: float = math.nan
    candle_value: float = math.nan
    delayed_price: float | None = None


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Result of sending one order to the (simulated) venue."""

    quantity: float  # filled quantity; 0.0 when nothing was executed
    price: float  # average fill price, costs of spread and impact included
    fee: float
    spread_cost: float = 0.0  # value lost to the half spread, in quote currency
    impact_cost: float = 0.0  # value lost to price impact, in quote currency
    status: str = "filled"  # filled | partial | rejected
    detail: str = ""


class ExecutionModel(ABC):
    """Turns (side, quantity, market price, conditions) into an outcome."""

    #: Extra candles an order waits before it is sent (0 = the next open).
    delay_bars: int = 0
    #: Loss applied when a position has to be liquidated because the asset stopped trading.
    delisting_haircut: float = 0.0

    @abstractmethod
    def cost_bound(self, conditions: MarketConditions | None) -> float:
        """Cost of a buy as a fraction of its value, for an order that takes the
        largest allowed share of a typical day's volume. Used to size orders and
        to check cash *before* the fill is known.

        It is a planning figure, not a guarantee: the fill itself is computed
        afterwards, and the broker shrinks a fill that would cost more than the
        cash in the account rather than let the account go negative."""

    @abstractmethod
    def execute(
        self,
        side: Side,
        quantity: float,
        market_price: float,
        conditions: MarketConditions | None,
        forced: bool = False,
    ) -> ExecutionOutcome:
        """Execute at ``market_price`` (the price before frictions). ``forced``
        marks a liquidation whose price already contains its own penalty."""


class LegacyExecution(ExecutionModel):
    """Fixed slippage, fixed fee, every order fills completely."""

    def __init__(self, costs: CostConfig) -> None:
        self.costs = costs

    def cost_bound(self, conditions: MarketConditions | None) -> float:
        return self.costs.cost_rate

    def execute(
        self,
        side: Side,
        quantity: float,
        market_price: float,
        conditions: MarketConditions | None,
        forced: bool = False,
    ) -> ExecutionOutcome:
        slip = self.costs.slippage_rate
        price = market_price * (1.0 + slip if side is Side.BUY else 1.0 - slip)
        return ExecutionOutcome(
            quantity=quantity,
            price=price,
            fee=quantity * price * self.costs.fee_rate,
            spread_cost=quantity * market_price * slip,
        )


class RealisticExecution(ExecutionModel):
    """Liquidity- and volatility-dependent execution (see the module docstring)."""

    def __init__(self, config: ExecutionConfig, costs: CostConfig) -> None:
        self.config = config
        self.fee_rate = costs.fee_rate
        self.delay_bars = config.delay_bars
        self.delisting_haircut = config.delisting_haircut
        self._rng = np.random.default_rng(config.seed)

    # ------------------------------------------------------------ components
    def half_spread(self, conditions: MarketConditions | None) -> float:
        """Estimated half spread as a fraction of price."""
        c = self.config
        value = conditions.traded_value if conditions is not None else math.nan
        if not (math.isfinite(value) and value > 0):
            return c.max_half_spread_bps / 1e4  # nothing known about liquidity: the worst case
        value *= c.liquidity_scale
        bps = c.half_spread_coefficient_bps * value**c.half_spread_exponent * c.spread_multiplier
        return float(min(bps, c.max_half_spread_bps)) / 1e4

    def impact(self, order_value: float, conditions: MarketConditions | None) -> float:
        """Estimated price impact of an order of ``order_value`` (square-root law)."""
        c = self.config
        if conditions is None:
            return 0.0
        value, sigma = conditions.traded_value * c.liquidity_scale, conditions.volatility
        if not (math.isfinite(value) and value > 0 and math.isfinite(sigma) and sigma > 0):
            return 0.0
        return c.impact_coefficient * sigma * math.sqrt(order_value / value)

    def cost_bound(self, conditions: MarketConditions | None) -> float:
        c = self.config
        sigma = conditions.volatility if conditions is not None else math.nan
        sigma = sigma if math.isfinite(sigma) and sigma > 0 else 0.10
        worst_impact = c.impact_coefficient * sigma * math.sqrt(c.max_participation)
        return (1.0 + self.half_spread(conditions) + worst_impact) * (1.0 + self.fee_rate) - 1.0

    # ---------------------------------------------------------------- execute
    def execute(
        self,
        side: Side,
        quantity: float,
        market_price: float,
        conditions: MarketConditions | None,
        forced: bool = False,
    ) -> ExecutionOutcome:
        c = self.config
        if forced:  # a liquidation at a price that already carries its penalty: fee only
            return ExecutionOutcome(quantity, market_price, quantity * market_price * self.fee_rate)

        if c.rejection_rate > 0 and self._rng.random() < c.rejection_rate:
            return ExecutionOutcome(
                0.0, market_price, 0.0, status="rejected", detail="venue rejected the order"
            )

        filled, status, detail = quantity, "filled", ""
        available = conditions.candle_value if conditions is not None else math.nan
        if math.isfinite(available):
            limit = available * c.liquidity_scale * c.max_participation / market_price
            if limit < filled:
                filled, status = limit, "partial"
                detail = f"only {c.max_participation:.1%} of the candle's volume is available"
        if filled * market_price < c.min_notional:
            return ExecutionOutcome(
                0.0,
                market_price,
                0.0,
                status="rejected",
                detail=f"below the minimum order value {c.min_notional:g}",
            )

        value = filled * market_price
        spread, impact = self.half_spread(conditions), self.impact(value, conditions)
        direction = 1.0 if side is Side.BUY else -1.0
        price = market_price * (1.0 + direction * (spread + impact))
        return ExecutionOutcome(
            quantity=filled,
            price=price,
            fee=filled * price * self.fee_rate,
            spread_cost=value * spread,
            impact_cost=value * impact,
            status=status,
            detail=detail,
        )


def build_execution(config: ExecutionConfig | None, costs: CostConfig) -> ExecutionModel:
    """The model a configuration asks for: realistic when an ``execution`` section
    is present, the legacy fixed-cost model otherwise."""
    return RealisticExecution(config, costs) if config is not None else LegacyExecution(costs)
