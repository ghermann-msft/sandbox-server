"""Add organization-scoped agent profiles

Revision ID: 023
Revises: 022
Create Date: 2026-11-18 10:15:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '023'
down_revision: str | None = '022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'organization_agent_profiles',
        sa.Column(
            'organization_id',
            sa.UUID(),
            sa.ForeignKey('organizations.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column('payload', sa.Text(), nullable=False),
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


def downgrade() -> None:
    op.drop_table('organization_agent_profiles')
