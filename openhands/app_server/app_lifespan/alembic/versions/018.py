"""Add global sandbox execution profiles

Revision ID: 018
Revises: 017
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '018'
down_revision: str | None = '017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'v1_sandbox_spec',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table('conversation_metadata') as batch_op:
        batch_op.add_column(sa.Column('sandbox_spec_id', sa.String(), nullable=True))
        batch_op.create_index(
            'ix_conversation_metadata_sandbox_spec_id',
            ['sandbox_spec_id'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('conversation_metadata') as batch_op:
        batch_op.drop_index('ix_conversation_metadata_sandbox_spec_id')
        batch_op.drop_column('sandbox_spec_id')
    op.drop_table('v1_sandbox_spec')
