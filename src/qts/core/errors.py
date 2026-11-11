"""Central exception hierarchy.

Every error raised on purpose by the platform derives from :class:`QtsError`, so
entry points (CLI, schedulers) can catch one type, log it once and exit cleanly,
while programming errors keep their original traceback.
"""

from __future__ import annotations


class QtsError(Exception):
    """Base class for all expected platform errors."""


class ConfigError(QtsError):
    """Configuration is missing, malformed or violates a safety rule."""


class LiveTradingDisabledError(QtsError):
    """Raised whenever something tries to reach a real-money code path."""


class DataError(QtsError):
    """Market data is missing, corrupt or fails a quality check."""


class RiskViolationError(QtsError):
    """An order tried to reach a broker without a valid risk approval."""


class ExecutionError(QtsError):
    """A broker could not execute an approved order."""


class PersistenceError(QtsError):
    """The operational database rejected or could not serve a request."""
