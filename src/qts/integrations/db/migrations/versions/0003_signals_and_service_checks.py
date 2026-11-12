"""Record of every strategy signal, and of every monitoring check.

``signals``: one row per symbol and candle of a paper session — what the
strategy said and what was done about it. ``service_checks``: one row per
health check per monitoring cycle, from which availability is computed.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("entry", sa.Boolean(), nullable=False),
        sa.Column("exit", sa.Boolean(), nullable=False),
        sa.Column("stop_distance", sa.Float(), nullable=True),
        sa.Column("in_position", sa.Boolean(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "ts", "symbol"),
    )
    op.create_table(
        "service_checks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("service_checks", schema=None) as batch_op:
        batch_op.create_index("ix_service_checks_ts", ["ts"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("service_checks", schema=None) as batch_op:
        batch_op.drop_index("ix_service_checks_ts")
    op.drop_table("service_checks")
    op.drop_table("signals")
