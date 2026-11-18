"""Use UTC server timestamps for organization agent profiles

Revision ID: 024
Revises: 023
Create Date: 2026-11-18 12:45:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '024'
down_revision: str | None = '023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        for column_name in ('created_at', 'updated_at'):
            op.alter_column(
                'organization_agent_profiles',
                column_name,
                existing_type=sa.DateTime(timezone=False),
                type_=sa.DateTime(timezone=True),
                existing_nullable=False,
                server_default=sa.func.now(),
                postgresql_using=f"{column_name} AT TIME ZONE 'UTC'",
            )
        return

    with op.batch_alter_table('organization_agent_profiles') as batch_op:
        for column_name in ('created_at', 'updated_at'):
            batch_op.alter_column(
                column_name,
                existing_type=sa.DateTime(),
                existing_nullable=False,
                server_default=sa.func.now(),
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        for column_name in ('created_at', 'updated_at'):
            op.alter_column(
                'organization_agent_profiles',
                column_name,
                existing_type=sa.DateTime(timezone=True),
                type_=sa.DateTime(timezone=False),
                existing_nullable=False,
                server_default=None,
                postgresql_using=f"{column_name} AT TIME ZONE 'UTC'",
            )
        return

    with op.batch_alter_table('organization_agent_profiles') as batch_op:
        for column_name in ('created_at', 'updated_at'):
            batch_op.alter_column(
                column_name,
                existing_type=sa.DateTime(),
                existing_nullable=False,
                server_default=None,
            )
