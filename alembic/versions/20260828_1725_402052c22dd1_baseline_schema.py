"""baseline schema

Consolidation baseline for the single canonical SQLAlchemy Base
(trading.database.connection.Base). Captures the schema that was
previously created by Base.metadata.create_all() across the two former
Bases: the 10 control-center tables plus the legacy strategy_heartbeats
table.

This revision is a faithful snapshot of the pre-existing schema -- it does
NOT change, rename, or drop anything. Redundant per-table id indexes
(ix_<table>_id, alongside the primary key) are kept deliberately so the
baseline matches what create_all() already produced in the live database.

Applying it:
  * Database that already has these tables (e.g. the production
    PostgreSQL instance): `alembic stamp head` -- records this revision
    as applied without running the DDL.
  * Fresh database (isolated tests, a new environment, docker-compose):
    `alembic upgrade head` -- creates every table.

Hand-adjusted after autogenerate: index creation uses plain
op.create_index / op.drop_index instead of batch_alter_table (this is a
create-from-empty baseline; no in-place ALTER semantics are needed, and
the plain form is dialect-neutral across PostgreSQL and SQLite).

Revision ID: 402052c22dd1
Revises:
Create Date: 2026-08-28 17:25:46.053833
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "402052c22dd1"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_hash", "window_start", name="uq_rate_limit_key_window"),
    )
    op.create_index("ix_rate_limit_windows_api_key_hash", "rate_limit_windows", ["api_key_hash"], unique=False)
    op.create_index("ix_rate_limit_windows_id", "rate_limit_windows", ["id"], unique=False)

    op.create_table(
        "servers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("ec2_instance_id", sa.String(length=32), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("os", sa.String(length=20), nullable=False),
        sa.Column("repo_path", sa.String(length=255), nullable=False),
        sa.Column("provisioning_status", sa.String(length=20), nullable=False),
        sa.Column("provisioning_message", sa.String(length=2000), nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_servers_id", "servers", ["id"], unique=False)
    op.create_index("ix_servers_name", "servers", ["name"], unique=True)

    op.create_table(
        "strategy_heartbeats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategy_name", sa.String(length=120), nullable=False),
        sa.Column("server_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_mtm", sa.Float(), nullable=False),
        sa.Column("day_pnl", sa.Float(), nullable=False),
        sa.Column("number_of_trades", sa.Integer(), nullable=False),
        sa.Column("last_update_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_name", "server_name", name="uq_strategy_server"),
    )
    op.create_index("ix_strategy_heartbeats_id", "strategy_heartbeats", ["id"], unique=False)
    op.create_index("ix_strategy_heartbeats_server_name", "strategy_heartbeats", ["server_name"], unique=False)
    op.create_index("ix_strategy_heartbeats_status", "strategy_heartbeats", ["status"], unique=False)
    op.create_index("ix_strategy_heartbeats_strategy_name", "strategy_heartbeats", ["strategy_name"], unique=False)

    op.create_table(
        "algos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("script_path", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "server_id", name="uq_algo_name_server"),
    )
    op.create_index("ix_algos_id", "algos", ["id"], unique=False)
    op.create_index("ix_algos_name", "algos", ["name"], unique=False)
    op.create_index("ix_algos_server_id", "algos", ["server_id"], unique=False)

    op.create_table(
        "algo_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("algo_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("exit_reason", sa.String(length=255), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["algo_id"], ["algos.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_algo_runs_algo_id", "algo_runs", ["algo_id"], unique=False)
    op.create_index("ix_algo_runs_id", "algo_runs", ["id"], unique=False)
    op.create_index("ix_algo_runs_server_id", "algo_runs", ["server_id"], unique=False)

    op.create_table(
        "commands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("algo_id", sa.Integer(), nullable=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("command", sa.String(length=20), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("requested_by", sa.String(length=120), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["algo_id"], ["algos.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_commands_algo_id", "commands", ["algo_id"], unique=False)
    op.create_index("ix_commands_id", "commands", ["id"], unique=False)
    op.create_index("ix_commands_job_id", "commands", ["job_id"], unique=False)
    op.create_index("ix_commands_server_id", "commands", ["server_id"], unique=False)

    op.create_table(
        "daily_pnl",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("algo_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["algo_id"], ["algos.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("algo_id", "server_id", "date", name="uq_daily_pnl_algo_server_date"),
    )
    op.create_index("ix_daily_pnl_algo_id", "daily_pnl", ["algo_id"], unique=False)
    op.create_index("ix_daily_pnl_date", "daily_pnl", ["date"], unique=False)
    op.create_index("ix_daily_pnl_id", "daily_pnl", ["id"], unique=False)
    op.create_index("ix_daily_pnl_server_id", "daily_pnl", ["server_id"], unique=False)

    op.create_table(
        "heartbeats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("algo_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("cpu", sa.Float(), nullable=True),
        sa.Column("memory", sa.Float(), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("position", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["algo_id"], ["algos.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_heartbeats_algo_id", "heartbeats", ["algo_id"], unique=False)
    op.create_index("ix_heartbeats_id", "heartbeats", ["id"], unique=False)
    op.create_index("ix_heartbeats_server_id", "heartbeats", ["server_id"], unique=False)
    op.create_index("ix_heartbeats_timestamp", "heartbeats", ["timestamp"], unique=False)

    op.create_table(
        "logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("algo_id", sa.Integer(), nullable=True),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["algo_id"], ["algos.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_logs_algo_id", "logs", ["algo_id"], unique=False)
    op.create_index("ix_logs_event", "logs", ["event"], unique=False)
    op.create_index("ix_logs_id", "logs", ["id"], unique=False)
    op.create_index("ix_logs_server_id", "logs", ["server_id"], unique=False)
    op.create_index("ix_logs_timestamp", "logs", ["timestamp"], unique=False)

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("algo_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("average_price", sa.Float(), nullable=False),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["algo_id"], ["algos.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("algo_id", "server_id", "symbol", name="uq_position_algo_server_symbol"),
    )
    op.create_index("ix_positions_algo_id", "positions", ["algo_id"], unique=False)
    op.create_index("ix_positions_id", "positions", ["id"], unique=False)
    op.create_index("ix_positions_server_id", "positions", ["server_id"], unique=False)

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("algo_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["algo_id"], ["algos.id"]),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trades_algo_id", "trades", ["algo_id"], unique=False)
    op.create_index("ix_trades_executed_at", "trades", ["executed_at"], unique=False)
    op.create_index("ix_trades_id", "trades", ["id"], unique=False)
    op.create_index("ix_trades_server_id", "trades", ["server_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_trades_server_id", table_name="trades")
    op.drop_index("ix_trades_id", table_name="trades")
    op.drop_index("ix_trades_executed_at", table_name="trades")
    op.drop_index("ix_trades_algo_id", table_name="trades")
    op.drop_table("trades")

    op.drop_index("ix_positions_server_id", table_name="positions")
    op.drop_index("ix_positions_id", table_name="positions")
    op.drop_index("ix_positions_algo_id", table_name="positions")
    op.drop_table("positions")

    op.drop_index("ix_logs_timestamp", table_name="logs")
    op.drop_index("ix_logs_server_id", table_name="logs")
    op.drop_index("ix_logs_id", table_name="logs")
    op.drop_index("ix_logs_event", table_name="logs")
    op.drop_index("ix_logs_algo_id", table_name="logs")
    op.drop_table("logs")

    op.drop_index("ix_heartbeats_timestamp", table_name="heartbeats")
    op.drop_index("ix_heartbeats_server_id", table_name="heartbeats")
    op.drop_index("ix_heartbeats_id", table_name="heartbeats")
    op.drop_index("ix_heartbeats_algo_id", table_name="heartbeats")
    op.drop_table("heartbeats")

    op.drop_index("ix_daily_pnl_server_id", table_name="daily_pnl")
    op.drop_index("ix_daily_pnl_id", table_name="daily_pnl")
    op.drop_index("ix_daily_pnl_date", table_name="daily_pnl")
    op.drop_index("ix_daily_pnl_algo_id", table_name="daily_pnl")
    op.drop_table("daily_pnl")

    op.drop_index("ix_commands_server_id", table_name="commands")
    op.drop_index("ix_commands_job_id", table_name="commands")
    op.drop_index("ix_commands_id", table_name="commands")
    op.drop_index("ix_commands_algo_id", table_name="commands")
    op.drop_table("commands")

    op.drop_index("ix_algo_runs_server_id", table_name="algo_runs")
    op.drop_index("ix_algo_runs_id", table_name="algo_runs")
    op.drop_index("ix_algo_runs_algo_id", table_name="algo_runs")
    op.drop_table("algo_runs")

    op.drop_index("ix_algos_server_id", table_name="algos")
    op.drop_index("ix_algos_name", table_name="algos")
    op.drop_index("ix_algos_id", table_name="algos")
    op.drop_table("algos")

    op.drop_index("ix_strategy_heartbeats_strategy_name", table_name="strategy_heartbeats")
    op.drop_index("ix_strategy_heartbeats_status", table_name="strategy_heartbeats")
    op.drop_index("ix_strategy_heartbeats_server_name", table_name="strategy_heartbeats")
    op.drop_index("ix_strategy_heartbeats_id", table_name="strategy_heartbeats")
    op.drop_table("strategy_heartbeats")

    op.drop_index("ix_servers_name", table_name="servers")
    op.drop_index("ix_servers_id", table_name="servers")
    op.drop_table("servers")

    op.drop_index("ix_rate_limit_windows_id", table_name="rate_limit_windows")
    op.drop_index("ix_rate_limit_windows_api_key_hash", table_name="rate_limit_windows")
    op.drop_table("rate_limit_windows")
