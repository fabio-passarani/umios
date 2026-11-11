"""System health checks (used by ``qts health`` and the Docker healthcheck)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from qts.core.config import AppConfig
from qts.core.errors import QtsError
from qts.integrations.db.storage import Storage
from qts.market_data.schema import TIMEFRAME_TO_FREQ
from qts.market_data.store import ParquetStore
from qts.risk.kill_switch import KillSwitch


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str


def run_health_checks(
    config: AppConfig,
    storage: Storage,
    store: ParquetStore,
    kill_switch: KillSwitch,
    now: datetime | None = None,
    max_heartbeat_age_hours: float = 26.0,
) -> list[HealthCheck]:
    """Run every check; never raises (a failing check is a result, not a crash)."""
    now = now or datetime.now(tz=UTC)
    checks: list[HealthCheck] = []

    db_ok = storage.ping()
    checks.append(HealthCheck("database", db_ok, "reachable" if db_ok else "unreachable"))

    market = config.market
    limit = pd.Timedelta(hours=config.risk.max_data_age_hours)
    for symbol in market.symbols:
        try:
            frame = store.read(market.exchange, symbol, market.timeframe)
            closed_at = frame.index[-1] + TIMEFRAME_TO_FREQ[market.timeframe]
            age = pd.Timestamp(now) - closed_at
            checks.append(
                HealthCheck(
                    f"data:{symbol}", age <= limit, f"last candle closed {closed_at:%Y-%m-%d %H:%M}"
                )
            )
        except QtsError as exc:
            checks.append(HealthCheck(f"data:{symbol}", False, str(exc)))

    state = kill_switch.state
    checks.append(
        HealthCheck(
            "kill_switch",
            not state.active,
            f"ACTIVE: {state.reason}" if state.active else "not active",
        )
    )

    if db_ok:
        try:
            for beat in storage.heartbeats():
                ts = beat.ts if beat.ts.tzinfo else beat.ts.replace(tzinfo=UTC)
                age_hours = (now - ts).total_seconds() / 3600.0
                healthy = beat.status == "ok" and age_hours <= max_heartbeat_age_hours
                checks.append(
                    HealthCheck(
                        f"service:{beat.service}",
                        healthy,
                        f"status={beat.status}, last seen {age_hours:.1f}h ago",
                    )
                )
        except QtsError as exc:
            checks.append(HealthCheck("heartbeats", False, str(exc)))
    return checks
