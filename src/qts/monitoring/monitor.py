"""The monitoring cycle: run the health checks, keep their history, write reports.

Runs as its own long-lived service (``qts monitor run``), separate from the
paper trader, so that a trader that hangs or dies is noticed by something that
is still alive. It only reads trading data; it never places or changes an order.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from qts.core.config import AppConfig
from qts.core.logging import get_logger
from qts.integrations.db.storage import Storage
from qts.market_data.store import ParquetStore
from qts.monitoring.daily import build_daily_report, render_daily_report
from qts.monitoring.health import HealthCheck, run_health_checks
from qts.risk.kill_switch import KillSwitch

log = get_logger("monitor")
SERVICE = "monitor"
#: A service that steps hourly must have been seen within this many hours.
MAX_HEARTBEAT_AGE_HOURS = 3.0


class Monitor:
    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        store: ParquetStore,
        kill_switch: KillSwitch,
        reports: Path | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.store = store
        self.kill_switch = kill_switch
        self.reports = reports
        self._failing: frozenset[str] | None = None

    def cycle(self, now: datetime | None = None) -> list[HealthCheck]:
        """One monitoring pass. Never raises for an unhealthy system: a failed
        check is a recorded result."""
        now = now or datetime.now(tz=UTC)
        checks = [
            c
            for c in run_health_checks(
                self.config,
                self.storage,
                self.store,
                self.kill_switch,
                now,
                max_heartbeat_age_hours=MAX_HEARTBEAT_AGE_HOURS,
            )
            if c.name != f"service:{SERVICE}"  # the monitor does not vouch for itself
        ]
        self.storage.add_service_checks(now, [(c.name, c.ok, c.detail) for c in checks])
        failing = frozenset(c.name for c in checks if not c.ok)
        if failing != self._failing:  # report changes, not every cycle
            if failing:
                detail = "; ".join(f"{c.name}: {c.detail}" for c in checks if not c.ok)
                self.storage.record_event("error", SERVICE, f"unhealthy: {detail}")
                log.error("unhealthy", failing=sorted(failing))
            elif self._failing:
                self.storage.record_event("info", SERVICE, "all checks healthy again")
                log.info("recovered")
            self._failing = failing
        self.storage.heartbeat(
            SERVICE, "ok", f"{len(checks) - len(failing)}/{len(checks)} checks healthy"
        )
        if self.reports is not None:
            try:
                self.write_reports(now)
            except OSError as exc:
                # The reports directory is a host mount: wrong ownership there must
                # not stop the monitoring. `qts report daily` still prints the report.
                log.error("daily_report_not_written", error=type(exc).__name__, detail=str(exc))
        return checks

    def write_reports(self, now: datetime, days: int = 3) -> list[Path]:
        """(Re)write the daily reports of the last ``days`` days of the active
        paper session. Rewriting is deliberate: the candle of day *D* is only
        processed on *D+1*, so a report fills in over time."""
        run = self.storage.active_paper_run()
        if run is None or self.reports is None:
            return []
        self.reports.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for back in range(days):
            day = (now - timedelta(days=back)).date()
            report = build_daily_report(self.storage, run, day, now)
            target = self.reports / f"{day.isoformat()}.md"
            target.write_text(render_daily_report(report), encoding="utf-8")
            target.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            written.append(target)
        return written
