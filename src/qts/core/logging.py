"""Structured logging with secret redaction.

structlog is used instead of the stdlib formatter because every event is a
dictionary: it can be rendered as JSON for machines (Docker, Grafana/Loki later)
or as coloured text for humans, and processors such as :func:`redact_secrets`
see the data *before* it becomes a string.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "***REDACTED***"

_SENSITIVE_KEY = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|private[_-]?key|authorization|credential|dsn"
    r"|database_url|signature)",
    re.IGNORECASE,
)
# user:password@host inside URLs (e.g. a database DSN embedded in an error message)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^@\s/]+@")


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _URL_CREDENTIALS.sub(rf"\g<scheme>{REDACTED}@", value)
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if _SENSITIVE_KEY.search(str(k)) else _scrub(v)) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_scrub(v) for v in value)
    return value


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: mask values of sensitive keys and credentials in URLs."""
    for key in list(event_dict):
        if _SENSITIVE_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _scrub(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog once per process."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> Any:
    """Return a lazy logger tagged with a component name.

    The proxy resolves the configuration on use, so module-level loggers created
    at import time still honour a later :func:`configure_logging` call.
    """
    return structlog.get_logger(component=name)
