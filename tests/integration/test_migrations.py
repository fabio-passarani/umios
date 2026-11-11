"""Schema versioning: fresh databases, databases that pre-date migrations, drift."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from qts.core.config import Settings
from qts.core.errors import ConfigError
from qts.integrations.db.models import Base
from qts.integrations.db.storage import Storage


def test_fresh_database_is_migrated_to_head(storage: Storage) -> None:
    assert storage.schema_revision() == Storage.head_revision()
    tables = set(inspect(storage.engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables


def test_migrations_and_models_describe_the_same_schema(storage: Storage) -> None:
    """If a model changes without a migration (or the reverse) this fails."""
    with storage.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)
    assert differences == []


def test_upgrade_is_idempotent(storage: Storage) -> None:
    run_id = storage.create_run("backtest", "backtest", "s", {}, "f", 1.0)
    assert storage.upgrade() == Storage.head_revision()
    storage.init_schema()
    assert storage.get_run(run_id) is not None


def test_database_created_before_migrations_is_adopted_not_recreated(tmp_path: Path) -> None:
    legacy = Storage(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    # what version 0.1.0 did: create_all, no version record
    first = Base.metadata.tables
    for name in (
        "runs",
        "equity_points",
        "fills",
        "trades",
        "positions",
        "risk_decisions",
        "risk_events",
        "system_events",
        "heartbeats",
        "paper_state",
    ):
        first[name].create(legacy.engine)
    with legacy.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs (id, kind, mode, strategy, status, config_fingerprint, config, "
                "initial_capital, started_at) VALUES ('old', 'paper', 'paper', 's', 'running', "
                "'f', '{}', 10000, '2026-01-01 00:00:00')"
            )
        )
    assert legacy.schema_revision() is None

    legacy.init_schema()

    assert legacy.schema_revision() == Storage.head_revision()
    run = legacy.get_run("old")
    assert run is not None  # the data is still there
    assert run.status == "running"


def test_database_url_is_built_from_parts_with_proper_escaping(tmp_path: Path) -> None:
    awkward = "p@ss:w/rd?#%"
    settings = Settings(_env_file=None, db_host="postgres", db_password=awkward)  # type: ignore[call-arg]
    url = settings.resolved_database_url()
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    assert (parsed.username, parsed.password, parsed.host, parsed.port, parsed.database) == (
        "qts_app",
        awkward,
        "postgres",
        5432,
        "qts",
    )
    assert awkward not in repr(settings)

    secret = tmp_path / "db_password"
    secret.write_text(awkward + "\n")
    from_file = Settings(_env_file=None, db_host="postgres", db_password_file=secret)  # type: ignore[call-arg]
    assert make_url(from_file.resolved_database_url()).password == awkward


def test_database_settings_precedence_and_errors(tmp_path: Path) -> None:
    assert Settings(_env_file=None).resolved_database_url() == "sqlite:///state/qts.sqlite"  # type: ignore[call-arg]
    explicit = Settings(_env_file=None, database_url="sqlite:///x.sqlite", db_host="postgres")  # type: ignore[call-arg]
    assert explicit.resolved_database_url() == "sqlite:///x.sqlite"
    with pytest.raises(ConfigError, match="QTS_DB_PASSWORD"):
        Settings(_env_file=None, db_host="postgres").resolved_database_url()  # type: ignore[call-arg]
    with pytest.raises(ConfigError, match="password file"):
        Settings(
            _env_file=None, db_host="postgres", db_password_file=tmp_path / "nope"
        ).resolved_database_url()  # type: ignore[call-arg]
