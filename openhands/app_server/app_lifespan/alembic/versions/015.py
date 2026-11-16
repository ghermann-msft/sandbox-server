"""Add hosted user settings, secrets, and GitHub OAuth state

Revision ID: 015
Revises: 014
Create Date: 2026-11-14 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '015'
down_revision: str | None = '014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'user_settings',
        sa.Column('user_id', sa.String(), primary_key=True),
        sa.Column('encrypted_payload', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_table(
        'user_secrets',
        sa.Column('user_id', sa.String(), primary_key=True),
        sa.Column('encrypted_payload', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_table(
        'github_oauth_state',
        sa.Column('nonce', sa.String(), primary_key=True),
        sa.Column('user_id', sa.String(), nullable=False, index=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('github_oauth_state')
    op.drop_table('user_secrets')
    op.drop_table('user_settings')
