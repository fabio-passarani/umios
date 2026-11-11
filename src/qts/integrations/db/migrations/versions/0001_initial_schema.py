"""Initial schema: the ten operational tables as they existed at version 0.1.0.

Databases created before migrations were introduced already contain these
tables; ``Storage.upgrade`` recognises them and marks this revision as applied
instead of running it.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "heartbeats",
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("service"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("initial_capital", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runs_kind"), ["kind"], unique=False)

    op.create_table(
        "system_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("system_events", schema=None) as batch_op:
        batch_op.create_index("ix_system_events_ts", ["ts"], unique=False)

    op.create_table(
        "equity_points",
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("exposure", sa.Float(), nullable=False),
        sa.Column("drawdown", sa.Float(), nullable=False),
        sa.Column("benchmark_equity", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "ts"),
    )
    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("fee", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("fills", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_fills_run_id"), ["run_id"], unique=False)

    op.create_table(
        "paper_state",
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "positions",
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("avg_price", sa.Float(), nullable=False),
        sa.Column("last_price", sa.Float(), nullable=False),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "symbol"),
    )
    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("reference_price", sa.Float(), nullable=False),
        sa.Column("order_reason", sa.String(length=32), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("failed_checks", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("risk_decisions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_risk_decisions_run_id"), ["run_id"], unique=False)

    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("risk_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_risk_events_run_id"), ["run_id"], unique=False)

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=False),
        sa.Column("fees", sa.Float(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=False),
        sa.Column("return_pct", sa.Float(), nullable=False),
        sa.Column("exit_reason", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("trades", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_trades_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("trades", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trades_run_id"))

    op.drop_table("trades")
    with op.batch_alter_table("risk_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_risk_events_run_id"))

    op.drop_table("risk_events")
    with op.batch_alter_table("risk_decisions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_risk_decisions_run_id"))

    op.drop_table("risk_decisions")
    op.drop_table("positions")
    op.drop_table("paper_state")
    with op.batch_alter_table("fills", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_fills_run_id"))

    op.drop_table("fills")
    op.drop_table("equity_points")
    with op.batch_alter_table("system_events", schema=None) as batch_op:
        batch_op.drop_index("ix_system_events_ts")

    op.drop_table("system_events")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_kind"))

    op.drop_table("runs")
    op.drop_table("heartbeats")
