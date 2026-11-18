"""Add organization-scoped connections

Revision ID: 022
Revises: 021
Create Date: 2026-11-18 01:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '022'
down_revision: str | None = '021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'organization_connections',
        sa.Column(
            'organization_id',
            sa.UUID(),
            sa.ForeignKey('organizations.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column('kind', sa.String(), primary_key=True),
        sa.Column('encrypted_payload', sa.Text(), nullable=False),
        sa.Column('public_metadata', sa.JSON(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'created_by_user_id',
            sa.UUID(),
            sa.ForeignKey('rbac_users.id'),
            nullable=False,
        ),
        sa.Column(
            'updated_by_user_id',
            sa.UUID(),
            sa.ForeignKey('rbac_users.id'),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'organization_llm_profiles',
        sa.Column(
            'organization_id',
            sa.UUID(),
            sa.ForeignKey('organizations.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column('encrypted_payload', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'created_by_user_id',
            sa.UUID(),
            sa.ForeignKey('rbac_users.id'),
            nullable=False,
        ),
        sa.Column(
            'updated_by_user_id',
            sa.UUID(),
            sa.ForeignKey('rbac_users.id'),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table('oss_github_oauth_state') as batch_op:
        batch_op.add_column(sa.Column('organization_id', sa.UUID(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('oss_github_oauth_state') as batch_op:
        batch_op.drop_column('organization_id')
    op.drop_table('organization_llm_profiles')
    op.drop_table('organization_connections')
