"""Add durable organization RBAC

Revision ID: 015
Revises: 014
Create Date: 2026-11-17 00:00:00.000000
"""

from typing import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = '015'
down_revision: str | None = '014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_IDS = {
    'owner': UUID('7251a038-7bb0-4c5c-b9e6-313514185383'),
    'admin': UUID('7785fefd-2dd4-4a56-a9ea-c73964838462'),
    'member': UUID('6886caf4-1e0a-4cf8-8984-5c0fcf8c1aa2'),
}
PERMISSION_IDS = {
    'organization:view': UUID('db59bd3c-7f10-44a7-a6cb-64bb0caa54a6'),
    'organization:manage': UUID('ca2b90a2-e6e8-4532-b52f-8ebf630eeeb4'),
    'organization:manage_members': UUID('13058afd-af80-4c4a-9741-c7b66b046f8c'),
    'conversation:create': UUID('426099fe-eec7-4387-9158-86a907c39837'),
    'conversation:read': UUID('082d6aaf-c302-4d2d-91ba-4f096199cc81'),
    'conversation:write': UUID('0815c412-77c1-4c35-92c7-d245651c9cce'),
    'hindsight:recall': UUID('7d35ed70-5785-4380-a4d1-b3e586c29be7'),
    'hindsight:retain': UUID('25b982af-b172-4da9-be78-3263ba7bb1f2'),
}


def upgrade() -> None:
    op.create_table(
        'rbac_users',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('identity_provider', sa.String(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('subject_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'identity_provider',
            'tenant_id',
            'subject_id',
            name='uq_rbac_users_identity',
        ),
    )
    op.create_table(
        'organization_roles',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'organization_permissions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'organizations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('is_visible', sa.Boolean(), nullable=False),
        sa.Column('personal_owner_user_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['personal_owner_user_id'], ['rbac_users.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('personal_owner_user_id'),
    )
    op.create_table(
        'organization_role_permissions',
        sa.Column('role_id', sa.UUID(), nullable=False),
        sa.Column('permission_id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ['permission_id'], ['organization_permissions.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['role_id'], ['organization_roles.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('role_id', 'permission_id'),
    )
    op.create_table(
        'organization_memberships',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('role_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['role_id'], ['organization_roles.id']),
        sa.ForeignKeyConstraint(['user_id'], ['rbac_users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'organization_id', 'user_id', name='uq_organization_membership'
        ),
    )
    op.create_index(
        'ix_organization_memberships_organization_id',
        'organization_memberships',
        ['organization_id'],
    )
    op.create_index(
        'ix_organization_memberships_user_id',
        'organization_memberships',
        ['user_id'],
    )
    with op.batch_alter_table('conversation_metadata') as batch_op:
        batch_op.add_column(sa.Column('organization_id', sa.UUID(), nullable=True))
        batch_op.create_foreign_key(
            'fk_conversation_metadata_organization_id',
            'organizations',
            ['organization_id'],
            ['id'],
        )
        batch_op.create_index(
            'ix_conversation_metadata_organization_id',
            ['organization_id'],
            unique=False,
        )

    role_table = sa.table(
        'organization_roles', sa.column('id', sa.UUID()), sa.column('name', sa.String())
    )
    permission_table = sa.table(
        'organization_permissions',
        sa.column('id', sa.UUID()),
        sa.column('name', sa.String()),
    )
    role_permission_table = sa.table(
        'organization_role_permissions',
        sa.column('role_id', sa.UUID()),
        sa.column('permission_id', sa.UUID()),
    )
    op.bulk_insert(
        role_table,
        [{'id': role_id, 'name': name} for name, role_id in ROLE_IDS.items()],
    )
    op.bulk_insert(
        permission_table,
        [
            {'id': permission_id, 'name': name}
            for name, permission_id in PERMISSION_IDS.items()
        ],
    )
    role_permissions = {
        'owner': set(PERMISSION_IDS),
        'admin': set(PERMISSION_IDS) - {'organization:manage'},
        'member': {
            'organization:view',
            'conversation:create',
            'conversation:read',
            'conversation:write',
            'hindsight:recall',
            'hindsight:retain',
        },
    }
    op.bulk_insert(
        role_permission_table,
        [
            {
                'role_id': ROLE_IDS[role],
                'permission_id': PERMISSION_IDS[permission],
            }
            for role, permissions in role_permissions.items()
            for permission in sorted(permissions)
        ],
    )


def downgrade() -> None:
    with op.batch_alter_table('conversation_metadata') as batch_op:
        batch_op.drop_index('ix_conversation_metadata_organization_id')
        batch_op.drop_constraint(
            'fk_conversation_metadata_organization_id', type_='foreignkey'
        )
        batch_op.drop_column('organization_id')
    op.drop_index(
        'ix_organization_memberships_user_id',
        table_name='organization_memberships',
    )
    op.drop_index(
        'ix_organization_memberships_organization_id',
        table_name='organization_memberships',
    )
    op.drop_table('organization_memberships')
    op.drop_table('organization_role_permissions')
    op.drop_table('organizations')
    op.drop_table('organization_permissions')
    op.drop_table('organization_roles')
    op.drop_table('rbac_users')
