"""Add durable Kubernetes sandbox ownership

Revision ID: 017
Revises: 016
Create Date: 2026-11-15 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '017'
down_revision: str | None = '016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _parse_entra_user_id(user_id: str) -> tuple[str, str] | None:
    prefix, separator, remainder = user_id.partition(':')
    tenant_id, nested_separator, object_id = remainder.partition(':')
    if (
        prefix != 'entra'
        or not separator
        or not nested_separator
        or not tenant_id
        or not object_id
        or ':' in object_id
    ):
        return None
    return tenant_id, object_id


def upgrade() -> None:
    access_table = op.create_table(
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

    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT sandbox_id, MIN(created_by_user_id) AS created_by_user_id
            FROM app_conversation_start_task
            WHERE sandbox_id IS NOT NULL
              AND created_by_user_id IS NOT NULL
            GROUP BY sandbox_id
            HAVING COUNT(DISTINCT created_by_user_id) = 1
            """
        )
    )
    owners = []
    for row in rows:
        identity = _parse_entra_user_id(row.created_by_user_id)
        if identity is None:
            continue
        tenant_id, object_id = identity
        owners.append(
            {
                'sandbox_id': row.sandbox_id,
                'tenant_id': tenant_id,
                'owner_object_id': object_id,
                'access_type': 'PERSONAL',
            }
        )
    if owners:
        op.bulk_insert(access_table, owners)


def downgrade() -> None:
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
