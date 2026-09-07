"""expiry_cycles: enforce one ACTIVE cycle per underlying (partial unique index)

Revision ID: 6815728a049f
Revises: b7c1f2a9d3e4
Create Date: 2026-09-07 14:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6815728a049f'
down_revision: Union[str, None] = 'b7c1f2a9d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_expiry_cycle_one_active_per_underlying",
        "expiry_cycles",
        ["underlying"],
        unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"),
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index("uq_expiry_cycle_one_active_per_underlying", table_name="expiry_cycles")
