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
