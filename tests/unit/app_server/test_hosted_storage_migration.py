import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_020_preserves_conflicting_legacy_tables():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    sa.Table(
        'user_settings',
        metadata,
        sa.Column('legacy_id', sa.Integer(), primary_key=True),
    )
    sa.Table(
        'user_secrets',
        metadata,
        sa.Column('legacy_key', sa.String(), primary_key=True),
    )
    sa.Table(
        'github_oauth_state',
        metadata,
        sa.Column('legacy_state', sa.String(), primary_key=True),
    )
    metadata.create_all(engine)

    migration = importlib.import_module(
        'openhands.app_server.app_lifespan.alembic.versions.020'
    )
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
        finally:
            migration.op = original_op

    inspector = sa.inspect(engine)
    assert {
        'user_settings',
        'user_secrets',
        'github_oauth_state',
        'oss_user_settings',
        'oss_user_secrets',
        'oss_github_oauth_state',
    }.issubset(inspector.get_table_names())
    assert {column['name'] for column in inspector.get_columns('user_settings')} == {
        'legacy_id'
    }
    assert {column['name'] for column in inspector.get_columns('user_secrets')} == {
        'legacy_key'
    }

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.downgrade()
        finally:
            migration.op = original_op

    remaining_tables = set(sa.inspect(engine).get_table_names())
    assert {
        'user_settings',
        'user_secrets',
        'github_oauth_state',
    }.issubset(remaining_tables)
    assert not {
        'oss_user_settings',
        'oss_user_secrets',
        'oss_github_oauth_state',
    }.intersection(remaining_tables)
