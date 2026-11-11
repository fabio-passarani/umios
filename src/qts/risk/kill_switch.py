"""Latching kill switch.

Once active it stays active — across restarts when a state file is used — until
a human resets it explicitly. It can be tripped automatically by the risk engine
(drawdown, repeated errors) or manually (``qts risk kill``).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from qts.core.logging import get_logger

log = get_logger("risk.kill_switch")


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    active: bool = False
    reason: str = ""
    source: str = ""
    activated_at: str | None = None


class KillSwitch:
    """Thread-unsafe by design (single trading loop); file-backed when ``path`` is set."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._state = self._load()

    def _load(self) -> KillSwitchState:
        if self._path is None or not self._path.is_file():
            return KillSwitchState()
        try:
            return KillSwitchState(**json.loads(self._path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            # An unreadable state file is itself an anomaly: fail closed.
            return KillSwitchState(
                active=True,
                reason="kill switch state file is corrupt",
                source="system",
                activated_at=datetime.now(tz=UTC).isoformat(),
            )

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self._state)), encoding="utf-8")
        os.replace(tmp, self._path)

    @property
    def state(self) -> KillSwitchState:
        return self._state

    @property
    def active(self) -> bool:
        return self._state.active

    def is_active_at(self, moment: datetime) -> bool:
        """True if the switch was already tripped at ``moment`` (used by paper replays)."""
        if not self._state.active:
            return False
        if self._state.activated_at is None:
            return True
        return moment >= datetime.fromisoformat(self._state.activated_at)

    def activate(self, reason: str, source: str, at: datetime | None = None) -> None:
        """Trip the switch. Idempotent: the first reason is kept."""
        if self._state.active:
            return
        moment = at or datetime.now(tz=UTC)
        self._state = KillSwitchState(True, reason, source, moment.isoformat())
        self._persist()
        log.error("kill_switch_activated", reason=reason, source=source, at=moment.isoformat())

    def reset(self, operator: str) -> None:
        """Manually clear the switch. Never called by automated code."""
        if not operator.strip():
            raise ValueError("an operator name is required to reset the kill switch")
        previous = self._state
        self._state = KillSwitchState()
        self._persist()
        log.warning("kill_switch_reset", operator=operator, previous_reason=previous.reason)
