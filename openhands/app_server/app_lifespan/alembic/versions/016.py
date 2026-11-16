"""Add collision-proof OSS hosted storage tables

Revision ID: 016
Revises: 015
Create Date: 2026-11-15 00:00:00.000000
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
        'oss_user_settings',
        sa.Column('user_id', sa.String(), primary_key=True),
        sa.Column('encrypted_payload', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_table(
        'oss_user_secrets',
        sa.Column('user_id', sa.String(), primary_key=True),
        sa.Column('encrypted_payload', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_table(
        'oss_github_oauth_state',
        sa.Column('nonce', sa.String(), primary_key=True),
        sa.Column('user_id', sa.String(), nullable=False, index=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('oss_github_oauth_state')
    op.drop_table('oss_user_secrets')
    op.drop_table('oss_user_settings')
