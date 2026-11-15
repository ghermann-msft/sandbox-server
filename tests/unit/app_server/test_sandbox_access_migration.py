import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_017_backfills_only_unambiguous_entra_sandbox_owners():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    start_tasks = sa.Table(
        'app_conversation_start_task',
        metadata,
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('sandbox_id', sa.String()),
        sa.Column('created_by_user_id', sa.String()),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            start_tasks.insert(),
            [
                {
                    'id': '1',
                    'sandbox_id': 'owned',
                    'created_by_user_id': 'entra:tenant-a:owner-a',
                },
                {
                    'id': '2',
                    'sandbox_id': 'owned',
                    'created_by_user_id': 'entra:tenant-a:owner-a',
                },
                {
                    'id': '3',
                    'sandbox_id': 'conflicted',
                    'created_by_user_id': 'entra:tenant-a:owner-a',
                },
                {
                    'id': '4',
                    'sandbox_id': 'conflicted',
                    'created_by_user_id': 'entra:tenant-b:owner-b',
                },
                {
                    'id': '5',
                    'sandbox_id': 'invalid',
                    'created_by_user_id': 'owner-without-entra-identity',
                },
            ],
        )

    migration = importlib.import_module(
        'openhands.app_server.app_lifespan.alembic.versions.017'
    )
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
        finally:
            migration.op = original_op

        owners = connection.execute(
            sa.text(
                """
                SELECT sandbox_id, tenant_id, owner_object_id
                FROM v1_sandbox_access
                """
            )
        ).mappings()
        assert list(owners) == [
            {
                'sandbox_id': 'owned',
                'tenant_id': 'tenant-a',
                'owner_object_id': 'owner-a',
            }
        ]

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.downgrade()
        finally:
            migration.op = original_op

    assert 'v1_sandbox_access' not in sa.inspect(engine).get_table_names()


def test_017_recovers_existing_unstamped_access_table():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    start_tasks = sa.Table(
        'app_conversation_start_task',
        metadata,
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('sandbox_id', sa.String()),
        sa.Column('created_by_user_id', sa.String()),
    )
    access = sa.Table(
        'v1_sandbox_access',
        metadata,
        sa.Column('sandbox_id', sa.String(), primary_key=True),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('owner_object_id', sa.String(), nullable=False),
        sa.Column('access_type', sa.String(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            access.insert(),
            {
                'sandbox_id': 'preserved',
                'tenant_id': 'tenant-a',
                'owner_object_id': 'owner-a',
                'access_type': 'PERSONAL',
            },
        )
        connection.execute(
            start_tasks.insert(),
            [
                {
                    'id': '1',
                    'sandbox_id': 'preserved',
                    'created_by_user_id': 'entra:other:owner',
                },
                {
                    'id': '2',
                    'sandbox_id': 'backfilled',
                    'created_by_user_id': 'entra:tenant-b:owner-b',
                },
            ],
        )

    migration = importlib.import_module(
        'openhands.app_server.app_lifespan.alembic.versions.017'
    )
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
        finally:
            migration.op = original_op

        owners = connection.execute(
            sa.select(
                access.c.sandbox_id,
                access.c.tenant_id,
                access.c.owner_object_id,
            ).order_by(access.c.sandbox_id)
        ).mappings()
        assert list(owners) == [
            {
                'sandbox_id': 'backfilled',
                'tenant_id': 'tenant-b',
                'owner_object_id': 'owner-b',
            },
            {
                'sandbox_id': 'preserved',
                'tenant_id': 'tenant-a',
                'owner_object_id': 'owner-a',
            },
        ]

    assert {
        index['name'] for index in sa.inspect(engine).get_indexes('v1_sandbox_access')
    } == {
        'ix_v1_sandbox_access_created_at',
        'ix_v1_sandbox_access_owner_object_id',
        'ix_v1_sandbox_access_tenant_id',
    }
