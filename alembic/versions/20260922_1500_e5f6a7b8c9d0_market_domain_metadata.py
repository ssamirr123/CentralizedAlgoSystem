"""Market domain metadata (Phase 7)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-22 15:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Section 34/35: all nullable -- every existing row gets NULL, which
    # repository._row_to_job() (and the backtest equivalent) interpret as
    # the exact pre-Phase-7 default (market=US/exchange=GENERIC/
    # currency=USD). No data backfill, no rewriting of legacy rows.
    with op.batch_alter_table("ai_research_runs") as batch_op:
        batch_op.add_column(sa.Column("market", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("exchange", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("canonical_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("currency", sa.String(length=8), nullable=True))
    op.create_index("ix_ai_research_runs_market", "ai_research_runs", ["market"])

    with op.batch_alter_table("ai_backtest_jobs") as batch_op:
        batch_op.add_column(sa.Column("market", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ai_backtest_jobs") as batch_op:
        batch_op.drop_column("market")

    op.drop_index("ix_ai_research_runs_market", table_name="ai_research_runs")
    with op.batch_alter_table("ai_research_runs") as batch_op:
        batch_op.drop_column("currency")
        batch_op.drop_column("canonical_id")
        batch_op.drop_column("exchange")
        batch_op.drop_column("market")
