"""Persist sandbox startup progress

Revision ID: 019
Revises: 018
Create Date: 2026-11-16 21:40:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '019'
down_revision: str | None = '018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('app_conversation_start_task') as batch_op:
        batch_op.add_column(
            sa.Column('sandbox_startup_progress', sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('app_conversation_start_task') as batch_op:
        batch_op.drop_column('sandbox_startup_progress')
