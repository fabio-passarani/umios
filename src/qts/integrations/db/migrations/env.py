"""Alembic environment. Always driven from code (see ``Storage.upgrade``), which
hands over an open connection, so no URL — and no password — is ever written to
an ini file."""

from __future__ import annotations

from alembic import context

from qts.integrations.db.models import Base

target_metadata = Base.metadata


def run_migrations() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:  # pragma: no cover - guarded by Storage.upgrade
        raise RuntimeError("migrations must be run through qts (`qts db upgrade`)")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite cannot ALTER in place
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
