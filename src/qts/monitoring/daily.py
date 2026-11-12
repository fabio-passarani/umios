"""Daily report and soak-test verdict for a paper-trading session.

Both are computed from the database only (signals, orders, fills, equity,
monitoring checks, events), so they describe what was actually recorded — not
what the process believes about itself.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from qts.integrations.db.models import Run
from qts.integrations.db.storage import Storage

#: A soak test is judged only after this many days of recorded operation.
SOAK_DAYS = 7
#: Minimum share of monitoring cycles in which every check passed.
SOAK_MIN_AVAILABILITY = 0.99
#: Longest tolerated silence between two monitoring cycles.
SOAK_MAX_GAP = timedelta(minutes=30)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


@dataclass
class Availability:
    cycles: int = 0
    healthy_cycles: int = 0
    longest_gap: timedelta = timedelta(0)
    failing: dict[str, int] = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        return self.healthy_cycles / self.cycles if self.cycles else 0.0


def availability(
    storage: Storage, start: datetime, end: datetime, ignore: tuple[str, ...] = ()
) -> Availability:
    """Group monitoring checks into cycles (same timestamp) and summarise them.

    ``longest_gap`` includes the silence before the first and after the last
    cycle of the window, so a monitor that died is visible.
    """
    result = Availability()
    cycles: dict[datetime, bool] = {}
    for check in storage.service_checks(start, end):
        if check.name in ignore:
            continue
        ts = _utc(check.ts)
        cycles[ts] = cycles.get(ts, True) and check.ok
        if not check.ok:
            result.failing[check.name] = result.failing.get(check.name, 0) + 1
    result.cycles = len(cycles)
    result.healthy_cycles = sum(cycles.values())
    edges = [start, *sorted(cycles), end]
    result.longest_gap = max((b - a for a, b in itertools.pairwise(edges)), default=end - start)
    return result


def build_daily_report(storage: Storage, run: Run, day: date, now: datetime) -> dict[str, Any]:
    """Everything that happened in ``run`` on ``day`` (UTC candle date)."""
    start, end = _day_bounds(day)
    window_end = min(end, now)

    def inside(ts: datetime) -> bool:
        return start <= _utc(ts) < end

    curve = storage.equity_curve(run.id)
    today = curve.loc[(curve.index >= start) & (curve.index < end)]
    before = curve.loc[curve.index < start]
    equity = float(today["equity"].iloc[-1]) if len(today) else None
    previous = float(before["equity"].iloc[-1]) if len(before) else run.initial_capital
    signals = [s for s in storage.signals(run.id) if inside(s.ts)]
    decisions = [d for d in storage.risk_decisions(run.id) if inside(d.ts)]
    fills = [f for f in storage.fills(run.id) if inside(f.ts)]
    trades = [t for t in storage.trades(run.id) if inside(t.exit_time)]
    events = storage.events_between(start, window_end)
    risk_events = [e for e in storage.risk_events(run.id) if inside(e.ts)]
    uptime = availability(storage, start, window_end)
    return {
        "date": day.isoformat(),
        "generated_at": now.isoformat(),
        "run_id": run.id,
        "mode": run.mode,
        "config_fingerprint": run.config_fingerprint,
        "candle_processed": bool(len(today)),
        "equity": equity,
        "previous_equity": previous,
        "day_pnl": (equity - previous) if equity is not None else None,
        "day_return": (equity / previous - 1.0) if equity is not None and previous else None,
        "total_return": (equity / run.initial_capital - 1.0) if equity is not None else None,
        "drawdown": float(today["drawdown"].iloc[-1]) if len(today) else None,
        "exposure": float(today["exposure"].iloc[-1]) if len(today) else None,
        "signals": [
            {
                "symbol": s.symbol,
                "close": s.close,
                "entry": s.entry,
                "exit": s.exit,
                "in_position": s.in_position,
                "action": s.action,
            }
            for s in signals
        ],
        "orders": [
            {
                "symbol": d.symbol,
                "side": d.side,
                "reason": d.order_reason,
                "quantity": d.quantity,
                "approved": d.approved,
                "rejected_by": d.failed_checks,
            }
            for d in decisions
        ],
        "fills": [
            {
                "symbol": f.symbol,
                "side": f.side,
                "quantity": f.quantity,
                "price": f.price,
                "fee": f.fee,
                "reason": f.reason,
            }
            for f in fills
        ],
        "closed_trades": [
            {"symbol": t.symbol, "pnl": t.pnl, "return_pct": t.return_pct, "reason": t.exit_reason}
            for t in trades
        ],
        "open_positions": [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "last_price": p.last_price,
                "stop_price": p.stop_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in storage.positions(run.id)
        ],
        "risk_events": [{"kind": e.kind, "detail": e.detail} for e in risk_events],
        "errors": [
            {"time": _utc(e.ts).strftime("%H:%M"), "component": e.component, "message": e.message}
            for e in events
            if e.level == "error"
        ],
        "warnings": sum(1 for e in events if e.level == "warning"),
        "monitoring": {
            "cycles": uptime.cycles,
            "healthy_cycles": uptime.healthy_cycles,
            "availability": uptime.ratio,
            "longest_gap_minutes": uptime.longest_gap.total_seconds() / 60.0,
            "failing_checks": uptime.failing,
        },
        "heartbeats": [
            {
                "service": b.service,
                "status": b.status,
                "age_hours": (now - _utc(b.ts)).total_seconds() / 3600.0,
                "detail": b.detail,
            }
            for b in storage.heartbeats()
        ],
    }


def render_daily_report(report: dict[str, Any]) -> str:
    """Markdown rendering of :func:`build_daily_report`."""

    def money(value: float | None) -> str:
        return "n/a" if value is None else f"{value:,.2f}"

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:+.2%}"

    m = report["monitoring"]
    lines = [
        f"# Paper trading — daily report {report['date']}",
        "",
        f"Run `{report['run_id']}` · mode **{report['mode']}** (simulated, no real orders) · "
        f"config `{report['config_fingerprint']}` · generated {report['generated_at'][:16]} UTC",
        "",
        "## Account",
        "",
    ]
    if report["candle_processed"]:
        lines += [
            f"- Equity: {money(report['equity'])} ({pct(report['day_return'])} on the day, "
            f"{pct(report['total_return'])} since start)",
            f"- Day P&L: {money(report['day_pnl'])} · exposure {money(report['exposure'])} · "
            f"drawdown {pct(report['drawdown'])}",
        ]
    else:
        lines.append(
            "- **The candle of this day has not been processed.** Either it is not closed or "
            "published yet, or the paper trader did not run: check the errors below."
        )
    lines += ["", "## Signals", ""]
    if report["signals"]:
        lines += [
            "| Symbol | Close | Entry | Exit | In position | Action |",
            "|---|---:|:-:|:-:|:-:|---|",
        ]
        lines += [
            f"| {s['symbol']} | {s['close']:,.2f} | {'yes' if s['entry'] else '—'} | "
            f"{'yes' if s['exit'] else '—'} | {'yes' if s['in_position'] else '—'} | "
            f"{s['action']} |"
            for s in report["signals"]
        ]
    else:
        lines.append("No signal was recorded for this day.")
    lines += ["", "## Orders and fills", ""]
    if report["orders"]:
        lines += ["| Symbol | Side | Reason | Quantity | Risk engine |", "|---|---|---|---:|---|"]
        lines += [
            f"| {o['symbol']} | {o['side']} | {o['reason']} | {o['quantity']:.6f} | "
            + ("approved" if o["approved"] else f"**rejected**: {o['rejected_by']}")
            + " |"
            for o in report["orders"]
        ]
    else:
        lines.append("No order was evaluated.")
    for f in report["fills"]:
        lines.append(
            f"- Fill: {f['side']} {f['quantity']:.6f} {f['symbol']} @ {f['price']:,.2f} "
            f"(fee {f['fee']:.2f}, {f['reason']})"
        )
    for t in report["closed_trades"]:
        lines.append(
            f"- Closed trade: {t['symbol']} {money(t['pnl'])} "
            f"({pct(t['return_pct'])}, {t['reason']})"
        )
    lines += ["", "## Open positions", ""]
    if report["open_positions"]:
        lines += [
            "| Symbol | Quantity | Entry | Last | Stop | Unrealised P&L |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        lines += [
            f"| {p['symbol']} | {p['quantity']:.6f} | {p['avg_price']:,.2f} | "
            f"{p['last_price']:,.2f} | "
            f"{money(p['stop_price'])} | {money(p['unrealized_pnl'])} |"
            for p in report["open_positions"]
        ]
    else:
        lines.append("None.")
    lines += [
        "",
        "## Service health",
        "",
        f"- Monitoring cycles: {m['cycles']} ({m['healthy_cycles']} fully healthy, "
        f"{m['availability']:.1%}); longest silence {m['longest_gap_minutes']:.0f} min",
    ]
    for name, count in sorted(m["failing_checks"].items()):
        lines.append(f"- Check `{name}` failed in {count} cycle(s)")
    for b in report["heartbeats"]:
        lines.append(
            f"- `{b['service']}`: {b['status']}, last seen {b['age_hours']:.1f} h ago "
            f"({b['detail']})"
        )
    lines += ["", "## Errors and risk events", ""]
    if not report["errors"] and not report["risk_events"]:
        lines.append(f"None ({report['warnings']} warning(s)).")
    lines += [f"- {e['time']} `{e['component']}` {e['message']}" for e in report["errors"]]
    lines += [f"- risk event `{e['kind']}`: {e['detail']}" for e in report["risk_events"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- soak test
def soak_status(storage: Storage, run: Run, now: datetime) -> dict[str, Any]:
    """Judge a continuous paper-trading run against explicit criteria.

    The verdict is ``IN PROGRESS`` until :data:`SOAK_DAYS` days of *recorded*
    monitoring exist; only then can it be ``PASSED`` or ``FAILED``. Time during
    which nothing was monitored does not count as verified.
    """
    started = _utc(run.started_at)
    elapsed = now - started
    uptime = availability(storage, started, now)
    fills = storage.fills(run.id)
    order_ids = [f.order_id for f in fills]
    curve = storage.equity_curve(run.id)
    candle_days = [ts.date() for ts in curve.index]
    expected_days = (candle_days[-1] - candle_days[0]).days + 1 if candle_days else 0
    signals = storage.signals(run.id)
    signal_times = {_utc(s.ts) for s in signals}
    candle_times = {ts.to_pydatetime() for ts in curve.index}
    events = storage.events_between(started, now)
    errors = [e for e in events if e.level == "error"]
    backups = [b for b in storage.heartbeats() if b.service == "backup"]
    backup_age = (now - _utc(backups[0].ts)) if backups else None
    live_orders = [d for d in storage.risk_decisions(run.id) if d.side not in ("buy", "sell")]

    criteria = [
        ("dry-run only", run.mode == "paper" and not live_orders, f"session mode is {run.mode!r}"),
        (
            f"monitored for {SOAK_DAYS} days",
            elapsed >= timedelta(days=SOAK_DAYS) and uptime.cycles > 0,
            f"{elapsed.total_seconds() / 86400:.2f} days since the session started",
        ),
        (
            f"availability >= {SOAK_MIN_AVAILABILITY:.0%}",
            uptime.ratio >= SOAK_MIN_AVAILABILITY,
            f"{uptime.healthy_cycles}/{uptime.cycles} healthy cycles ({uptime.ratio:.2%})",
        ),
        (
            f"no monitoring gap > {SOAK_MAX_GAP.total_seconds() / 60:.0f} min",
            uptime.cycles > 0 and uptime.longest_gap <= SOAK_MAX_GAP,
            f"longest silence {uptime.longest_gap.total_seconds() / 60:.0f} min",
        ),
        (
            "a candle processed for every day",
            bool(candle_days) and len(candle_days) == expected_days,
            f"{len(candle_days)} candles over {expected_days} calendar days",
        ),
        (
            "every candle has its signals recorded",
            bool(signals) and candle_times <= signal_times,
            f"{len(signals)} signal rows",
        ),
        (
            "no duplicated order",
            len(order_ids) == len(set(order_ids)),
            f"{len(order_ids)} fills, {len(set(order_ids))} distinct orders",
        ),
        ("no operational error", not errors, f"{len(errors)} error event(s)"),
        (
            "backups are being taken",
            backup_age is not None
            and backup_age <= timedelta(hours=36)
            and backups[0].status == "ok",
            "no backup heartbeat"
            if backup_age is None
            else f"last backup {backup_age.total_seconds() / 3600:.1f} h ago ({backups[0].status})",
        ),
    ]
    complete = elapsed >= timedelta(days=SOAK_DAYS)
    passed = all(ok for _name, ok, _detail in criteria)
    verdict = "IN PROGRESS" if not complete else ("PASSED" if passed else "FAILED")
    return {
        "run_id": run.id,
        "started_at": started.isoformat(),
        "elapsed_days": elapsed.total_seconds() / 86400.0,
        "required_days": SOAK_DAYS,
        "verdict": verdict,
        "criteria": [{"name": n, "ok": ok, "detail": d} for n, ok, d in criteria],
        "failing_now": [n for n, ok, _d in criteria if not ok],
    }
