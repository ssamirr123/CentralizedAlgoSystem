"""straddle pulse: expiry cycles, daily sessions, oi snapshots

Revision ID: b7c1f2a9d3e4
Revises: 323f5b753c2c
Create Date: 2026-09-07 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c1f2a9d3e4'
down_revision: Union[str, None] = '323f5b753c2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('expiry_cycles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('underlying', sa.String(length=24), nullable=False),
    sa.Column('exchange', sa.String(length=8), nullable=False),
    sa.Column('expiry_date', sa.Date(), nullable=False),
    sa.Column('cycle_start_date', sa.Date(), nullable=False),
    sa.Column('cycle_end_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('underlying', 'expiry_date', name='uq_expiry_cycle_underlying_expiry'),
    )
    with op.batch_alter_table('expiry_cycles', schema=None) as batch_op:
        batch_op.create_index('ix_expiry_cycle_underlying_status', ['underlying', 'status'], unique=False)
        batch_op.create_index(batch_op.f('ix_expiry_cycles_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_expiry_cycles_underlying'), ['underlying'], unique=False)
        batch_op.create_index(batch_op.f('ix_expiry_cycles_expiry_date'), ['expiry_date'], unique=False)

    op.create_table('daily_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cycle_id', sa.Integer(), nullable=False),
    sa.Column('underlying', sa.String(length=24), nullable=False),
    sa.Column('trading_date', sa.Date(), nullable=False),
    sa.Column('spot_0916', sa.Float(), nullable=True),
    sa.Column('atm_strike', sa.Float(), nullable=True),
    sa.Column('atm_ce_contract_id', sa.Integer(), nullable=True),
    sa.Column('atm_pe_contract_id', sa.Integer(), nullable=True),
    sa.Column('atm_ce_symbol', sa.String(length=80), nullable=True),
    sa.Column('atm_pe_symbol', sa.String(length=80), nullable=True),
    sa.Column('session_status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['cycle_id'], ['expiry_cycles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['atm_ce_contract_id'], ['option_contracts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['atm_pe_contract_id'], ['option_contracts.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('underlying', 'trading_date', name='uq_daily_session_underlying_date'),
    )
    with op.batch_alter_table('daily_sessions', schema=None) as batch_op:
        batch_op.create_index('ix_daily_session_cycle', ['cycle_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_daily_sessions_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_daily_sessions_underlying'), ['underlying'], unique=False)
        batch_op.create_index(batch_op.f('ix_daily_sessions_trading_date'), ['trading_date'], unique=False)

    op.create_table('oi_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('underlying', sa.String(length=24), nullable=False),
    sa.Column('expiry_date', sa.Date(), nullable=False),
    sa.Column('trading_date', sa.Date(), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('call_oi_total', sa.Integer(), nullable=False),
    sa.Column('put_oi_total', sa.Integer(), nullable=False),
    sa.Column('call_oi_change', sa.Integer(), nullable=False),
    sa.Column('put_oi_change', sa.Integer(), nullable=False),
    sa.Column('pcr', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint(
        'underlying', 'expiry_date', 'trading_date', 'timestamp',
        name='uq_oi_snapshot_underlying_expiry_date_ts',
    ),
    )
    with op.batch_alter_table('oi_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_oi_snapshot_underlying_date', ['underlying', 'trading_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_oi_snapshots_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_oi_snapshots_underlying'), ['underlying'], unique=False)
        batch_op.create_index(batch_op.f('ix_oi_snapshots_trading_date'), ['trading_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_oi_snapshots_timestamp'), ['timestamp'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('oi_snapshots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_oi_snapshots_timestamp'))
        batch_op.drop_index(batch_op.f('ix_oi_snapshots_trading_date'))
        batch_op.drop_index(batch_op.f('ix_oi_snapshots_underlying'))
        batch_op.drop_index(batch_op.f('ix_oi_snapshots_id'))
        batch_op.drop_index('ix_oi_snapshot_underlying_date')
    op.drop_table('oi_snapshots')

    with op.batch_alter_table('daily_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_daily_sessions_trading_date'))
        batch_op.drop_index(batch_op.f('ix_daily_sessions_underlying'))
        batch_op.drop_index(batch_op.f('ix_daily_sessions_id'))
        batch_op.drop_index('ix_daily_session_cycle')
    op.drop_table('daily_sessions')

    with op.batch_alter_table('expiry_cycles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_expiry_cycles_expiry_date'))
        batch_op.drop_index(batch_op.f('ix_expiry_cycles_underlying'))
        batch_op.drop_index(batch_op.f('ix_expiry_cycles_id'))
        batch_op.drop_index('ix_expiry_cycle_underlying_status')
    op.drop_table('expiry_cycles')
