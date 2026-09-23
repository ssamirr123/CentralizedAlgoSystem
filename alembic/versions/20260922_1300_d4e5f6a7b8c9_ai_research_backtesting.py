"""AI Research Backtesting persistence (Phase 6)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-22 13:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_backtest_jobs",
        sa.Column("backtest_id", sa.String(length=36), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("selected_analysts", sa.JSON(), nullable=True),
        sa.Column("research_depth", sa.String(length=16), nullable=False),
        sa.Column("llm_provider", sa.String(length=32), nullable=True),
        sa.Column("quick_think_llm", sa.String(length=64), nullable=True),
        sa.Column("deep_think_llm", sa.String(length=64), nullable=True),
        sa.Column("debate_rounds", sa.Integer(), nullable=True),
        sa.Column("risk_debate_rounds", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=True),
        sa.Column("data_vendors", sa.JSON(), nullable=True),
        sa.Column("holding_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_runs", sa.Integer(), nullable=False),
        sa.Column("completed_runs", sa.Integer(), nullable=False),
        sa.Column("failed_runs", sa.Integer(), nullable=False),
        sa.Column("current_symbol", sa.String(length=32), nullable=True),
        sa.Column("current_date", sa.Date(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("backtest_id"),
    )
    op.create_index("ix_ai_backtest_jobs_status", "ai_backtest_jobs", ["status"])
    op.create_index("ix_ai_backtest_jobs_llm_provider", "ai_backtest_jobs", ["llm_provider"])
    op.create_index("ix_ai_backtest_jobs_created_at", "ai_backtest_jobs", ["created_at"])

    op.create_table(
        "ai_backtest_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("backtest_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("cell_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("research_id", sa.String(length=36), nullable=True),
        sa.Column("raw_decision", sa.String(length=2000), nullable=True),
        sa.Column("normalized_decision", sa.String(length=20), nullable=True),
        sa.Column("evaluation_status", sa.String(length=20), nullable=True),
        sa.Column("raw_return", sa.Float(), nullable=True),
        sa.Column("alpha_return", sa.Float(), nullable=True),
        sa.Column("benchmark", sa.String(length=16), nullable=True),
        sa.Column("holding_days", sa.Integer(), nullable=True),
        sa.Column("resolution_date", sa.Date(), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["backtest_id"], ["ai_backtest_jobs.backtest_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("backtest_id", "symbol", "cell_date", name="uq_ai_backtest_run_cell"),
    )
    op.create_index("ix_ai_backtest_runs_id", "ai_backtest_runs", ["id"])
    op.create_index("ix_ai_backtest_runs_backtest_id", "ai_backtest_runs", ["backtest_id"])
    op.create_index("ix_ai_backtest_runs_symbol", "ai_backtest_runs", ["symbol"])
    op.create_index("ix_ai_backtest_runs_cell_date", "ai_backtest_runs", ["cell_date"])
    op.create_index("ix_ai_backtest_runs_status", "ai_backtest_runs", ["status"])
    op.create_index("ix_ai_backtest_runs_research_id", "ai_backtest_runs", ["research_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_backtest_runs_research_id", table_name="ai_backtest_runs")
    op.drop_index("ix_ai_backtest_runs_status", table_name="ai_backtest_runs")
    op.drop_index("ix_ai_backtest_runs_cell_date", table_name="ai_backtest_runs")
    op.drop_index("ix_ai_backtest_runs_symbol", table_name="ai_backtest_runs")
    op.drop_index("ix_ai_backtest_runs_backtest_id", table_name="ai_backtest_runs")
    op.drop_index("ix_ai_backtest_runs_id", table_name="ai_backtest_runs")
    op.drop_table("ai_backtest_runs")

    op.drop_index("ix_ai_backtest_jobs_created_at", table_name="ai_backtest_jobs")
    op.drop_index("ix_ai_backtest_jobs_llm_provider", table_name="ai_backtest_jobs")
    op.drop_index("ix_ai_backtest_jobs_status", table_name="ai_backtest_jobs")
    op.drop_table("ai_backtest_jobs")
