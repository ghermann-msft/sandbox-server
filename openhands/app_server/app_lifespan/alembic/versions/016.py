"""Create Hindsight durable outbox

Revision ID: 016
Revises: 015
Create Date: 2026-11-17 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '016'
down_revision: str | None = '015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'hindsight_outbox',
        sa.Column('event_id', sa.String(length=64), primary_key=True),
        sa.Column('bank_id', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_error', sa.String(length=2000), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        'ix_hindsight_outbox_status',
        'hindsight_outbox',
        ['status'],
    )
    op.create_index(
        'ix_hindsight_outbox_next_attempt_at',
        'hindsight_outbox',
        ['next_attempt_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_hindsight_outbox_next_attempt_at',
        table_name='hindsight_outbox',
    )
    op.drop_index('ix_hindsight_outbox_status', table_name='hindsight_outbox')
    op.drop_table('hindsight_outbox')
