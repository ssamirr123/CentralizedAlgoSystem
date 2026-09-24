"""Market data provenance on AI research runs (Phase 8)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-22 17:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ai_research_runs") as batch_op:
        batch_op.add_column(sa.Column("market_data_provenance", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ai_research_runs") as batch_op:
        batch_op.drop_column("market_data_provenance")
