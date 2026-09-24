"""AI Options Research persistence (Phase 10)

Revision ID: a1b2c3d4e5f6
Revises: f6a7b8c9d0e1
Create Date: 2026-09-23 09:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_options_research_runs",
        sa.Column("research_id", sa.String(length=36), nullable=False),
        sa.Column("underlying", sa.String(length=32), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_stage", sa.String(length=80), nullable=True),
        sa.Column("llm_provider", sa.String(length=32), nullable=True),
        sa.Column("quick_think_llm", sa.String(length=64), nullable=True),
        sa.Column("deep_think_llm", sa.String(length=64), nullable=True),
        sa.Column("debate_rounds", sa.Integer(), nullable=False),
        sa.Column("candidate_limit", sa.Integer(), nullable=False),
        sa.Column("strategy_universe", sa.JSON(), nullable=True),
        sa.Column("generation_config", sa.JSON(), nullable=True),
        sa.Column("research_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("evidence_quality", sa.String(length=20), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("error_category", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("research_id"),
    )
    op.create_index("ix_ai_options_research_runs_underlying", "ai_options_research_runs", ["underlying"])
    op.create_index("ix_ai_options_research_runs_status", "ai_options_research_runs", ["status"])

    op.create_table(
        "ai_options_research_stages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("research_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["research_id"], ["ai_options_research_runs.research_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("research_id", "sequence", name="uq_ai_options_stage_research_seq"),
    )
    op.create_index("ix_ai_options_research_stages_id", "ai_options_research_stages", ["id"])
    op.create_index("ix_ai_options_research_stages_research_id", "ai_options_research_stages", ["research_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_options_research_stages_research_id", table_name="ai_options_research_stages")
    op.drop_index("ix_ai_options_research_stages_id", table_name="ai_options_research_stages")
    op.drop_table("ai_options_research_stages")

    op.drop_index("ix_ai_options_research_runs_status", table_name="ai_options_research_runs")
    op.drop_index("ix_ai_options_research_runs_underlying", table_name="ai_options_research_runs")
    op.drop_table("ai_options_research_runs")
