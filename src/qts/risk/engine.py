"""The independent risk engine.

Design rules:

1. **Independence** — it imports nothing from ``qts.strategies``. It judges an
   order purely from the order itself, the account state and its own limits, so
   a strategy bug cannot weaken a check.
2. **Veto power** — a rejection is final; there is no override parameter.
3. **Non-bypassable** — brokers execute only a :class:`RiskApproval` that this
   engine signed (HMAC) and has not yet consumed. An approval is bound to what
   was evaluated: it is valid only immediately, only at the approved price, and
   a newer entry approval revokes any unused older one.
4. **Fail closed** — any doubt (bad numbers, stale data, tripped kill switch,
   live mode) rejects. Risk-*reducing* sells stay possible so positions can
   always be closed.
5. **Everything is journaled** — approvals and rejections, with every check.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from qts.core.config import RiskLimits
from qts.core.errors import RiskViolationError
from qts.core.types import Order, OrderReason, Side, TradingMode
from qts.risk.journal import RiskJournal
from qts.risk.kill_switch import ENGINE_SOURCE, KillSwitch
from qts.risk.models import CheckResult, RiskApproval, RiskDecision

_TOLERANCE = 1e-9

#: How long an approval may wait before execution. Simulated brokers execute in
#: the same instant; anything older was decided on an account state that may no
#: longer exist and must be evaluated again.
APPROVAL_TTL = timedelta(minutes=1)
#: Relative difference tolerated between the approved and the executed entry price.
_PRICE_TOLERANCE = 1e-9


def _is_price(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Snapshot of the account and market the engine evaluates an order against.

    ``positions`` maps symbol -> held quantity; ``prices`` maps symbol -> latest
    trusted price (previous close); ``data_time`` is the timestamp of the newest
    market data and ``now`` the decision time (equal in a backtest).
    """

    now: datetime
    cash: float
    positions: dict[str, float]
    prices: dict[str, float]
    data_time: datetime
    cost_rate: float = 0.0
    pending_buy_notional: float = 0.0
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def exposure(self) -> float:
        return sum(q * self.prices.get(s, 0.0) for s, q in self.positions.items())

    @property
    def equity(self) -> float:
        return self.cash + self.exposure


class RiskEngine:
    """Pre-trade checks, account-level circuit breakers and the kill switch."""

    def __init__(
        self,
        limits: RiskLimits,
        mode: TradingMode,
        kill_switch: KillSwitch | None = None,
        journal: RiskJournal | None = None,
    ) -> None:
        self.limits = limits
        self.mode = mode
        self.kill_switch = kill_switch or KillSwitch()
        self.journal = journal or RiskJournal()
        self._key = secrets.token_bytes(32)
        self._outstanding: dict[str, Side] = {}
        self._consumed: set[str] = set()
        self._peak_equity: float | None = None
        self._last_equity: float | None = None
        self._last_equity_date: date | None = None
        self._day_start_equity: float | None = None
        self._daily_halt = False
        self._orders_today: dict[date, int] = {}
        self._consecutive_errors = 0

    # ------------------------------------------------------------------ state
    @property
    def daily_halt(self) -> bool:
        """True while new entries are blocked by the daily loss limit."""
        return self._daily_halt

    @property
    def drawdown(self) -> float:
        """Current drawdown from the equity peak, as a positive fraction."""
        if not self._peak_equity or self._last_equity is None:
            return 0.0
        return max(0.0, 1.0 - self._last_equity / self._peak_equity)

    def on_equity_update(self, timestamp: datetime, equity: float) -> None:
        """Feed the latest account equity (call at every mark-to-market).

        Updates the daily-loss halt and trips the kill switch on a drawdown breach.
        """
        if not math.isfinite(equity) or equity <= 0:
            self._trip(timestamp, f"invalid account equity: {equity!r}", "equity_anomaly")
            return
        today = timestamp.date()
        if self._last_equity_date is None:
            self._day_start_equity = equity
        elif today != self._last_equity_date:
            self._day_start_equity = self._last_equity
        self._last_equity = equity
        self._last_equity_date = today
        self._peak_equity = equity if self._peak_equity is None else max(self._peak_equity, equity)

        day_start = self._day_start_equity or equity
        daily_loss = 1.0 - equity / day_start
        breached = daily_loss >= self.limits.max_daily_loss_pct
        if breached and not self._daily_halt:
            self.journal.record_event(
                timestamp, "daily_loss_halt", f"daily loss {daily_loss:.2%} >= limit"
            )
        self._daily_halt = breached

        if self.drawdown >= self.limits.max_drawdown_pct:
            self._trip(
                timestamp,
                f"drawdown {self.drawdown:.2%} >= limit {self.limits.max_drawdown_pct:.2%}",
                "max_drawdown",
            )

    def record_error(self, timestamp: datetime, detail: str) -> None:
        """Report an operational failure; repeated failures trip the kill switch."""
        self._consecutive_errors += 1
        self.journal.record_event(timestamp, "error", detail)
        if self._consecutive_errors >= self.limits.max_consecutive_errors:
            self._trip(
                timestamp, f"{self._consecutive_errors} consecutive errors", "repeated_errors"
            )

    def record_success(self) -> None:
        """Report a healthy cycle (resets the consecutive error counter)."""
        self._consecutive_errors = 0

    def rebase_drawdown(self, timestamp: datetime, operator: str) -> None:
        """Operator action after a kill-switch reset: measure drawdown from the
        current equity again. Without it the drawdown check would keep vetoing
        entries forever, because a flat account cannot recover on its own."""
        if not operator.strip():
            raise ValueError("an operator name is required")
        self._peak_equity = self._last_equity
        self.journal.record_event(timestamp, "drawdown_rebased", f"by {operator}")

    def snapshot(self) -> dict[str, object]:
        """Serialisable account-level state (persisted between paper-trading steps)."""
        return {
            "peak_equity": self._peak_equity,
            "last_equity": self._last_equity,
            "last_equity_date": (
                self._last_equity_date.isoformat() if self._last_equity_date else None
            ),
            "day_start_equity": self._day_start_equity,
            "daily_halt": self._daily_halt,
            "orders_today": {d.isoformat(): n for d, n in self._orders_today.items()},
            "consecutive_errors": self._consecutive_errors,
        }

    def restore(self, state: dict[str, object]) -> None:
        """Inverse of :meth:`snapshot`."""

        def _num(key: str) -> float | None:
            value = state.get(key)
            return float(value) if isinstance(value, int | float) else None

        self._peak_equity = _num("peak_equity")
        self._last_equity = _num("last_equity")
        raw_date = state.get("last_equity_date")
        self._last_equity_date = date.fromisoformat(raw_date) if isinstance(raw_date, str) else None
        self._day_start_equity = _num("day_start_equity")
        self._daily_halt = bool(state.get("daily_halt", False))
        orders = state.get("orders_today", {})
        self._orders_today = (
            {date.fromisoformat(str(d)): int(n) for d, n in orders.items()}
            if isinstance(orders, dict)
            else {}
        )
        errors = state.get("consecutive_errors", 0)
        self._consecutive_errors = int(errors) if isinstance(errors, int) else 0

    def _trip(self, timestamp: datetime, reason: str, kind: str) -> None:
        if not self.kill_switch.active:
            self.journal.record_event(timestamp, f"kill_switch:{kind}", reason)
        self.kill_switch.activate(reason, source=ENGINE_SOURCE, at=timestamp)

    # ------------------------------------------------------------- evaluation
    def evaluate(self, order: Order, context: RiskContext) -> RiskDecision:
        """Run every applicable check and return a journaled decision."""
        checks = self._sanity_checks(order, context)
        if all(c.passed for c in checks):
            if order.side is Side.BUY:
                checks += self._entry_checks(order, context)
            else:
                checks += self._exit_checks(order, context)
        approved = all(c.passed for c in checks)
        approval = self._issue(order, context.now) if approved else None
        decision = RiskDecision(order, approved, tuple(checks), context.now, approval)
        if approved and order.side is Side.BUY:
            day = context.now.date()
            self._orders_today[day] = self._orders_today.get(day, 0) + 1
        self.journal.record(decision)
        return decision

    def _sanity_checks(self, order: Order, ctx: RiskContext) -> list[CheckResult]:
        numbers = [order.quantity, order.reference_price, ctx.cash, *ctx.positions.values()]
        if order.stop_price is not None:
            numbers.append(order.stop_price)
        valid = all(math.isfinite(v) for v in numbers)
        positive = (
            valid
            and order.quantity > 0
            and order.reference_price > 0
            and ctx.cash >= 0
            and all(q >= -_TOLERANCE for q in ctx.positions.values())
        )
        allowed = self.limits.allowed_symbols
        return [
            CheckResult(
                "trading_mode",
                self.mode in (TradingMode.BACKTEST, TradingMode.PAPER),
                "" if self.mode is not TradingMode.LIVE else "live trading is disabled",
            ),
            CheckResult(
                "order_sanity",
                bool(positive),
                "" if positive else "non-finite or negative quantity/price/cash/position",
            ),
            CheckResult(
                "symbol_allowed",
                allowed is None or order.symbol in allowed,
                "" if allowed is None or order.symbol in allowed else f"{order.symbol} not allowed",
            ),
        ]

    def _exit_checks(self, order: Order, ctx: RiskContext) -> list[CheckResult]:
        held = ctx.positions.get(order.symbol, 0.0)
        covered = order.quantity <= held + _TOLERANCE
        return [
            CheckResult(
                "no_short_selling",
                covered,
                "" if covered else f"sell {order.quantity} exceeds position {held}",
            )
        ]

    def _entry_checks(self, order: Order, ctx: RiskContext) -> list[CheckResult]:
        lim = self.limits
        equity = ctx.equity
        results: list[CheckResult] = []

        def add(name: str, passed: bool, detail: str) -> None:
            results.append(CheckResult(name, bool(passed), "" if passed else detail))

        killed = self.kill_switch.is_active_at(ctx.now)
        add("kill_switch", not killed, f"kill switch active: {self.kill_switch.state.reason}")
        add("entry_reason", order.reason is OrderReason.ENTRY, "buy orders must be entries")
        add("daily_loss_limit", not self._daily_halt, "daily loss limit reached: entries halted")
        add(
            "max_drawdown",
            self.drawdown < lim.max_drawdown_pct,
            f"drawdown {self.drawdown:.2%} >= {lim.max_drawdown_pct:.2%}",
        )

        age_hours = (ctx.now - ctx.data_time).total_seconds() / 3600.0
        add(
            "data_freshness",
            0 <= age_hours <= lim.max_data_age_hours,
            f"market data is {age_hours:.1f}h old (limit {lim.max_data_age_hours}h)",
        )

        last_price = ctx.prices.get(order.symbol)
        if last_price is None or not _is_price(last_price):
            add("price_available", False, "no trusted reference price for symbol")
            return results
        # Every open position must be valued: an unpriced one would silently
        # disappear from exposure and equity, loosening every percentage limit.
        unpriced = sorted(
            s
            for s, q in ctx.positions.items()
            if q > _TOLERANCE and not _is_price(ctx.prices.get(s))
        )
        if unpriced:
            add("positions_priced", False, f"no trusted price for open position(s) {unpriced}")
            return results
        deviation = abs(order.reference_price / last_price - 1.0)
        add(
            "price_deviation",
            deviation <= lim.max_price_deviation_pct,
            f"price deviates {deviation:.2%} from last close (limit "
            f"{lim.max_price_deviation_pct:.2%})",
        )

        if equity <= 0:
            add("equity_positive", False, "account equity is not positive")
            return results

        if order.stop_price is None:
            add("stop_loss_required", not lim.require_stop_loss, "entry has no stop loss")
        else:
            valid_stop = 0 < order.stop_price < order.reference_price
            add("stop_loss_valid", valid_stop, "stop must be below the entry price")
            if valid_stop:
                risk = order.quantity * (order.reference_price - order.stop_price) / equity
                add(
                    "max_risk_per_trade",
                    risk <= lim.max_risk_per_trade_pct + _TOLERANCE,
                    f"trade risks {risk:.2%} of equity (limit {lim.max_risk_per_trade_pct:.2%})",
                )

        held_value = ctx.positions.get(order.symbol, 0.0) * last_price
        position_pct = (held_value + order.notional) / equity
        add(
            "max_position_size",
            position_pct <= lim.max_position_pct + _TOLERANCE,
            f"position would be {position_pct:.2%} of equity (limit {lim.max_position_pct:.2%})",
        )

        exposure_pct = (ctx.exposure + ctx.pending_buy_notional + order.notional) / equity
        add(
            "max_total_exposure",
            exposure_pct <= lim.max_total_exposure_pct + _TOLERANCE,
            f"exposure would be {exposure_pct:.2%} (limit {lim.max_total_exposure_pct:.2%})",
        )

        open_positions = {s for s, q in ctx.positions.items() if q > _TOLERANCE}
        add(
            "max_open_positions",
            len(open_positions | {order.symbol}) <= lim.max_open_positions,
            f"would exceed {lim.max_open_positions} open positions",
        )

        required = order.notional * (1.0 + ctx.cost_rate)
        add(
            "cash_available",
            required <= ctx.cash + _TOLERANCE,
            f"needs {required:.2f} incl. costs, cash is {ctx.cash:.2f}",
        )

        count = self._orders_today.get(ctx.now.date(), 0)
        add(
            "max_orders_per_day",
            count < lim.max_orders_per_day,
            f"{count} entries already approved today (limit {lim.max_orders_per_day})",
        )
        return results

    # -------------------------------------------------------------- approvals
    def _sign(self, order: Order, issued_at: datetime) -> str:
        message = "|".join(
            [
                order.id,
                order.symbol,
                order.side.value,
                repr(order.quantity),
                repr(order.reference_price),
                repr(order.stop_price),
                order.reason.value,
                order.strategy,
                order.created_at.isoformat(),
                issued_at.isoformat(),
            ]
        )
        return hmac.new(self._key, message.encode(), hashlib.sha256).hexdigest()

    def _issue(self, order: Order, issued_at: datetime) -> RiskApproval:
        approval = RiskApproval(order, issued_at, self._sign(order, issued_at))
        if order.side is Side.BUY:
            # Each entry is approved against a snapshot of the account. Two unused
            # entry approvals granted on the same snapshot would both be executable
            # and together exceed the limits each was checked against, so a new
            # entry approval revokes every older unused one.
            self._outstanding = {i: s for i, s in self._outstanding.items() if s is not Side.BUY}
        self._outstanding[order.id] = order.side
        return approval

    def consume(self, approval: RiskApproval, market_price: float, timestamp: datetime) -> Order:
        """Validate an approval for execution at ``market_price`` / ``timestamp``
        and mark it used. Brokers must call this before executing.

        Raises :class:`RiskViolationError` for anything that is not a genuine,
        unmodified, unused, still-current approval issued by *this* engine, or
        for an execution that differs from what was approved:

        * the approval is older than :data:`APPROVAL_TTL` (or from the future);
        * an entry would fill at a price other than the approved one (a higher
          price means more notional and more risk than was checked);
        * the kill switch tripped between approval and execution (entries only:
          closing a position must always stay possible).

        A failed attempt still burns the approval: the caller has to ask again.
        """
        if not isinstance(approval, RiskApproval):
            raise RiskViolationError("order has no risk approval")
        expected = self._sign(approval.order, approval.issued_at)
        if not hmac.compare_digest(expected, approval.signature):
            raise RiskViolationError("risk approval signature is invalid (forged or altered)")
        order = approval.order
        if order.id in self._consumed:
            raise RiskViolationError("risk approval was already used (replay)")
        if order.id not in self._outstanding:
            raise RiskViolationError(
                "risk approval is not valid here (issued by another engine, or revoked by a "
                "newer entry approval)"
            )
        del self._outstanding[order.id]
        self._consumed.add(order.id)

        try:
            age = timestamp - approval.issued_at
        except TypeError as exc:  # naive vs aware datetimes: cannot prove freshness
            raise RiskViolationError("risk approval age cannot be determined") from exc
        if age < timedelta(0) or age > APPROVAL_TTL:
            raise RiskViolationError(
                f"risk approval expired (issued {approval.issued_at.isoformat()}, "
                f"executed {timestamp.isoformat()}): evaluate the order again"
            )
        if order.side is Side.BUY:
            if not isinstance(market_price, int | float) or not _is_price(market_price):
                raise RiskViolationError(f"invalid execution price {market_price!r}")
            if abs(market_price / order.reference_price - 1.0) > _PRICE_TOLERANCE:
                raise RiskViolationError(
                    f"execution price {market_price} differs from the approved price "
                    f"{order.reference_price}: evaluate the order again"
                )
            if self.kill_switch.is_active_at(timestamp):
                raise RiskViolationError("kill switch tripped after approval: entry cancelled")
        return order
