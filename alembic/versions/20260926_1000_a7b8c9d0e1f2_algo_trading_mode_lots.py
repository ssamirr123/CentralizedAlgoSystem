"""algos.trading_mode + algos.running_lots (latest values reported by heartbeat)

Revision ID: a7b8c9d0e1f2
Revises: b2c3d4e5f6a7
Create Date: 2026-09-26 10:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("algos", sa.Column("trading_mode", sa.String(length=10), nullable=True))
    op.add_column("algos", sa.Column("running_lots", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("algos", "running_lots")
    op.drop_column("algos", "trading_mode")
