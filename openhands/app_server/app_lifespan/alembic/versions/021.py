"""Create conversation cost event storage

Revision ID: 021
Revises: 020
Create Date: 2026-11-18 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '021'
down_revision: str | None = '020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'conversation_cost_events',
        sa.Column('id', sa.Integer(), sa.Identity(), nullable=False),
        sa.Column('conversation_id', sa.String(), nullable=False),
        sa.Column('cost_delta', sa.Float(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('usage_id', sa.String(), nullable=True),
        sa.Column('llm_model', sa.String(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ['conversation_id'],
            ['conversation_metadata.conversation_id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_conversation_cost_events_conversation_id'),
        'conversation_cost_events',
        ['conversation_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_conversation_cost_events_occurred_at'),
        'conversation_cost_events',
        ['occurred_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_conversation_cost_events_occurred_at'),
        table_name='conversation_cost_events',
    )
    op.drop_index(
        op.f('ix_conversation_cost_events_conversation_id'),
        table_name='conversation_cost_events',
    )
    op.drop_table('conversation_cost_events')
