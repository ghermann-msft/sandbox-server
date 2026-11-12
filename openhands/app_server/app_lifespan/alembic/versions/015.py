"""Add sandbox ownership and member ACLs

Revision ID: 015
Revises: 014
Create Date: 2026-11-12 00:00:00.000000
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
        'v1_sandbox_access',
        sa.Column('sandbox_id', sa.String(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('owner_object_id', sa.String(), nullable=False),
        sa.Column(
            'access_type', sa.String(), nullable=False, server_default='PERSONAL'
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('sandbox_id'),
    )
    op.create_index(
        op.f('ix_v1_sandbox_access_tenant_id'),
        'v1_sandbox_access',
        ['tenant_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_v1_sandbox_access_owner_object_id'),
        'v1_sandbox_access',
        ['owner_object_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_v1_sandbox_access_created_at'),
        'v1_sandbox_access',
        ['created_at'],
        unique=False,
    )
    op.create_table(
        'v1_sandbox_member',
        sa.Column('sandbox_id', sa.String(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('member_object_id', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ['sandbox_id'],
            ['v1_sandbox_access.sandbox_id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('sandbox_id', 'tenant_id', 'member_object_id'),
    )
    op.create_index(
        op.f('ix_v1_sandbox_member_member_object_id'),
        'v1_sandbox_member',
        ['member_object_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_v1_sandbox_member_member_object_id'),
        table_name='v1_sandbox_member',
    )
    op.drop_table('v1_sandbox_member')
    op.drop_index(
        op.f('ix_v1_sandbox_access_created_at'),
        table_name='v1_sandbox_access',
    )
    op.drop_index(
        op.f('ix_v1_sandbox_access_owner_object_id'),
        table_name='v1_sandbox_access',
    )
    op.drop_index(
        op.f('ix_v1_sandbox_access_tenant_id'),
        table_name='v1_sandbox_access',
    )
    op.drop_table('v1_sandbox_access')
