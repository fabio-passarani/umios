import json

import pytest

from qts.core.logging import REDACTED, configure_logging, get_logger, redact_secrets


def test_sensitive_keys_are_redacted() -> None:
    event = redact_secrets(
        None,
        "info",
        {
            "event": "x",
            "api_key": "abc",
            "API_SECRET": "def",
            "password": "p",
            "nested": {"token": "t", "ok": 1, "list": [{"private_key": "k"}]},
        },
    )
    assert event["api_key"] == REDACTED
    assert event["API_SECRET"] == REDACTED
    assert event["password"] == REDACTED
    assert event["nested"] == {"token": REDACTED, "ok": 1, "list": [{"private_key": REDACTED}]}


def test_credentials_inside_urls_are_redacted() -> None:
    event = redact_secrets(
        None, "error", {"event": "cannot connect to postgresql+psycopg://qts:hunter2@db:5432/qts"}
    )
    assert "hunter2" not in event["event"]
    assert "db:5432/qts" in event["event"]


def test_end_to_end_log_line_contains_no_secret(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_output=True)
    get_logger("test").info("login", exchange_api_secret="s3cr3t", url="https://u:pw@host/x")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert "s3cr3t" not in line
    assert "pw@" not in line
    assert payload["component"] == "test"
    configure_logging("CRITICAL", json_output=True)
