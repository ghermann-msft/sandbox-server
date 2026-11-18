import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_019_adds_and_removes_sandbox_startup_progress():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    sa.Table(
        'app_conversation_start_task',
        metadata,
        sa.Column('id', sa.String(), primary_key=True),
    )
    metadata.create_all(engine)

    migration = importlib.import_module(
        'openhands.app_server.app_lifespan.alembic.versions.019'
    )
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            columns = {
                column['name']
                for column in sa.inspect(connection).get_columns(
                    'app_conversation_start_task'
                )
            }
            assert 'sandbox_startup_progress' in columns

            migration.downgrade()
            columns = {
                column['name']
                for column in sa.inspect(connection).get_columns(
                    'app_conversation_start_task'
                )
            }
            assert 'sandbox_startup_progress' not in columns
        finally:
            migration.op = original_op
