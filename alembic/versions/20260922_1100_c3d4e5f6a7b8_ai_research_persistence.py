"""ai research: durable run + stage persistence (Phase 5)

Revision ID: c3d4e5f6a7b8
Revises: 116d41dff471
Create Date: 2026-09-22 11:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = '116d41dff471'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('ai_research_runs',
    sa.Column('research_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('research_date', sa.Date(), nullable=False),
    sa.Column('research_depth', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('stage', sa.String(length=20), nullable=False),
    sa.Column('current_stage', sa.String(length=80), nullable=True),
    sa.Column('llm_provider', sa.String(length=32), nullable=True),
    sa.Column('quick_think_llm', sa.String(length=64), nullable=True),
    sa.Column('deep_think_llm', sa.String(length=64), nullable=True),
    sa.Column('selected_analysts', sa.JSON(), nullable=True),
    sa.Column('data_vendors', sa.JSON(), nullable=True),
    sa.Column('debate_rounds', sa.Integer(), nullable=True),
    sa.Column('risk_debate_rounds', sa.Integer(), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=True),
    sa.Column('checkpoint_enabled', sa.Boolean(), nullable=False),
    sa.Column('portfolio', sa.JSON(), nullable=True),
    sa.Column('report', sa.JSON(), nullable=True),
    sa.Column('error_category', sa.String(length=32), nullable=True),
    sa.Column('error_message', sa.String(length=1000), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('research_id'),
    )
    with op.batch_alter_table('ai_research_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_research_runs_symbol'), ['symbol'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_research_runs_research_date'), ['research_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_research_runs_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_research_runs_llm_provider'), ['llm_provider'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_research_runs_created_at'), ['created_at'], unique=False)

    op.create_table('ai_research_stages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('research_id', sa.String(length=36), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['research_id'], ['ai_research_runs.research_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('research_id', 'sequence', name='uq_ai_research_stage_run_sequence'),
    )
    with op.batch_alter_table('ai_research_stages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_research_stages_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_research_stages_research_id'), ['research_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('ai_research_stages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_research_stages_research_id'))
        batch_op.drop_index(batch_op.f('ix_ai_research_stages_id'))
    op.drop_table('ai_research_stages')

    with op.batch_alter_table('ai_research_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_research_runs_created_at'))
        batch_op.drop_index(batch_op.f('ix_ai_research_runs_llm_provider'))
        batch_op.drop_index(batch_op.f('ix_ai_research_runs_status'))
        batch_op.drop_index(batch_op.f('ix_ai_research_runs_research_date'))
        batch_op.drop_index(batch_op.f('ix_ai_research_runs_symbol'))
    op.drop_table('ai_research_runs')
