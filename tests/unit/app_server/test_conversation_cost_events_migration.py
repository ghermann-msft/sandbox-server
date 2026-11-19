import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_021_adds_and_removes_conversation_cost_events():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    sa.Table(
        'conversation_metadata',
        metadata,
        sa.Column('conversation_id', sa.String(), primary_key=True),
    )
    metadata.create_all(engine)

    migration = importlib.import_module(
        'openhands.app_server.app_lifespan.alembic.versions.021'
    )
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            inspector = sa.inspect(connection)
            assert 'conversation_cost_events' in inspector.get_table_names()
            assert {
                'id',
                'conversation_id',
                'cost_delta',
                'occurred_at',
                'usage_id',
                'llm_model',
                'prompt_tokens',
                'completion_tokens',
            } == {
                column['name']
                for column in inspector.get_columns('conversation_cost_events')
            }
            assert {
                index['name']
                for index in inspector.get_indexes('conversation_cost_events')
            } == {
                'ix_conversation_cost_events_conversation_id',
                'ix_conversation_cost_events_occurred_at',
            }

            migration.downgrade()
            assert (
                'conversation_cost_events'
                not in sa.inspect(connection).get_table_names()
            )
        finally:
            migration.op = original_op
