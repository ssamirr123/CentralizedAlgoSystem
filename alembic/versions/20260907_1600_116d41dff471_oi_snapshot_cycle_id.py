"""oi_snapshots: add explicit cycle_id association

Revision ID: 116d41dff471
Revises: 6815728a049f
Create Date: 2026-09-07 16:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '116d41dff471'
down_revision: Union[str, None] = '6815728a049f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('oi_snapshots', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cycle_id', sa.Integer(), nullable=True))

    # Backfill any pre-existing rows by matching (underlying, expiry_date)
    # to the owning cycle -- straightforward since a cycle's identity is
    # exactly that pair (uq_expiry_cycle_underlying_expiry).
    op.execute(
        """
        UPDATE oi_snapshots
        SET cycle_id = (
            SELECT ec.id FROM expiry_cycles ec
            WHERE ec.underlying = oi_snapshots.underlying
              AND ec.expiry_date = oi_snapshots.expiry_date
        )
        """
    )

    with op.batch_alter_table('oi_snapshots', schema=None) as batch_op:
        batch_op.alter_column('cycle_id', nullable=False)
        batch_op.create_index('ix_oi_snapshot_cycle', ['cycle_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_oi_snapshot_cycle_id', 'expiry_cycles', ['cycle_id'], ['id'], ondelete='CASCADE',
        )


def downgrade() -> None:
    with op.batch_alter_table('oi_snapshots', schema=None) as batch_op:
        batch_op.drop_constraint('fk_oi_snapshot_cycle_id', type_='foreignkey')
        batch_op.drop_index('ix_oi_snapshot_cycle')
        batch_op.drop_column('cycle_id')
