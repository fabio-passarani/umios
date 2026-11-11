"""Latching kill switch.

Once active it stays active — across restarts when a state file is used — until
a human resets it explicitly. It can be tripped automatically by the risk engine
(drawdown, repeated errors) or manually (``qts risk kill``).

The state file is the single source of truth shared by every process (paper
trader, Freqtrade bot, CLI): it is re-read whenever it changes on disk, so a
``qts risk kill`` issued from another process or container stops a service that
is already running, without a restart.
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


#: ``source`` used by the risk engine for automatic trips. Those carry the
#: *simulated* time of the breach; every other source is a human or system
#: action in wall-clock time.
ENGINE_SOURCE = "risk_engine"

_Stamp = tuple[int, int, int] | None


class KillSwitch:
    """Thread-unsafe by design (single trading loop); file-backed when ``path`` is set."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._stamp: _Stamp = None
        self._state = KillSwitchState()
        self._refresh(force=True)

    def _file_stamp(self) -> _Stamp:
        """Cheap change detector for the state file (None when it does not exist)."""
        if self._path is None:
            return None
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _refresh(self, force: bool = False) -> None:
        """Re-read the state file if another process changed it since the last read."""
        if self._path is None:
            return
        try:
            stamp = self._file_stamp()
            if not force and stamp == self._stamp:
                return
            self._state = self._parse(self._path) if stamp is not None else KillSwitchState()
            self._stamp = stamp
        except OSError as exc:
            # A state file that exists but cannot be read is an anomaly: fail closed.
            self._state = _corrupt(f"kill switch state file is unreadable ({type(exc).__name__})")
            self._stamp = None

    @staticmethod
    def _parse(path: Path) -> KillSwitchState:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            state = KillSwitchState(**raw)
            if not isinstance(state.active, bool):
                raise TypeError("active must be a boolean")
            if state.activated_at is not None:
                datetime.fromisoformat(state.activated_at)
            return state
        except (ValueError, TypeError):
            # An unparsable state file is itself an anomaly: fail closed.
            return _corrupt("kill switch state file is corrupt")

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self._state)), encoding="utf-8")
        os.replace(tmp, self._path)
        self._stamp = self._file_stamp()

    @property
    def state(self) -> KillSwitchState:
        self._refresh()
        return self._state

    @property
    def active(self) -> bool:
        return self.state.active

    def is_active_at(self, moment: datetime) -> bool:
        """True if entries must be blocked for a decision stamped ``moment``.

        A trip by the risk engine carries the *simulated* time of the breach, so
        a replay of the same candles after a restart reproduces the same
        decisions before that instant. Any other activation (operator, corrupt
        file…) is a wall-clock action and blocks unconditionally: candles are
        stamped with their open time, which is always earlier than the moment a
        human pulls the switch, and must not slip through because of it.
        """
        state = self.state
        if not state.active:
            return False
        if state.source != ENGINE_SOURCE or state.activated_at is None:
            return True
        return moment >= datetime.fromisoformat(state.activated_at)

    def activate(self, reason: str, source: str, at: datetime | None = None) -> None:
        """Trip the switch. Idempotent: the first reason is kept."""
        if self.state.active:
            return
        moment = at or datetime.now(tz=UTC)
        self._state = KillSwitchState(True, reason, source, moment.isoformat())
        self._persist()
        log.error("kill_switch_activated", reason=reason, source=source, at=moment.isoformat())

    def reset(self, operator: str) -> None:
        """Manually clear the switch. Never called by automated code."""
        if not operator.strip():
            raise ValueError("an operator name is required to reset the kill switch")
        previous = self.state
        self._state = KillSwitchState()
        self._persist()
        log.warning("kill_switch_reset", operator=operator, previous_reason=previous.reason)


def _corrupt(reason: str) -> KillSwitchState:
    return KillSwitchState(
        active=True,
        reason=reason,
        source="system",
        activated_at=datetime.now(tz=UTC).isoformat(),
    )
