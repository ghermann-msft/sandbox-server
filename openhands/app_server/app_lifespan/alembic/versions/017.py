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


def _access_table(metadata: sa.MetaData) -> sa.Table:
    return sa.Table(
        'v1_sandbox_access',
        metadata,
        sa.Column('sandbox_id', sa.String(), primary_key=True),
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
    )


def _ensure_access_table() -> sa.Table:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    metadata = sa.MetaData()
    expected_table = _access_table(metadata)
    if not inspector.has_table(expected_table.name):
        expected_table.create(bind)
    else:
        actual_columns = {
            column['name'] for column in inspector.get_columns(expected_table.name)
        }
        expected_columns = set(expected_table.columns.keys())
        if not expected_columns.issubset(actual_columns):
            missing = sorted(expected_columns - actual_columns)
            raise RuntimeError(
                f'Existing {expected_table.name} table is missing columns: {missing}'
            )
        metadata.clear()
        expected_table = sa.Table(expected_table.name, metadata, autoload_with=bind)

    existing_indexes = {
        index['name'] for index in sa.inspect(bind).get_indexes(expected_table.name)
    }
    for name, column in (
        ('ix_v1_sandbox_access_tenant_id', 'tenant_id'),
        ('ix_v1_sandbox_access_owner_object_id', 'owner_object_id'),
        ('ix_v1_sandbox_access_created_at', 'created_at'),
    ):
        if name not in existing_indexes:
            op.create_index(name, expected_table.name, [column], unique=False)
    return expected_table


def upgrade() -> None:
    access_table = _ensure_access_table()
    bind = op.get_bind()
    existing_sandbox_ids = set(
        bind.execute(sa.select(access_table.c.sandbox_id)).scalars()
    )
    rows = bind.execute(
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
        if row.sandbox_id in existing_sandbox_ids:
            continue
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
