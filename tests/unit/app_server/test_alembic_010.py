import importlib
import io
from unittest.mock import MagicMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_composite_index_uses_transactional_ddl(monkeypatch):
    migration = importlib.import_module(
        'openhands.app_server.app_lifespan.alembic.versions.010'
    )
    create_index = MagicMock()
    drop_index = MagicMock()

    monkeypatch.setattr(migration.op, 'create_index', create_index)
    monkeypatch.setattr(migration.op, 'drop_index', drop_index)

    migration.upgrade()

    create_index.assert_called_once_with(
        'ix_event_callback_conversation_id_status_event_kind',
        'event_callback',
        ['conversation_id', 'status', 'event_kind'],
        if_not_exists=True,
    )

    migration.downgrade()

    drop_index.assert_called_once_with(
        'ix_event_callback_conversation_id_status_event_kind',
        table_name='event_callback',
        if_exists=True,
    )


def _compile_postgresql_upgrade(revision: str) -> str:
    migration = importlib.import_module(
        f'openhands.app_server.app_lifespan.alembic.versions.{revision}'
    )
    output = io.StringIO()
    context = MigrationContext.configure(
        url='postgresql://',
        opts={'as_sql': True, 'output_buffer': output},
    )

    with Operations.context(context):
        migration.upgrade()

    return output.getvalue()


def test_composite_index_postgresql_ddl_is_transactional():
    sql = _compile_postgresql_upgrade('010')

    assert sql == (
        'CREATE INDEX IF NOT EXISTS '
        'ix_event_callback_conversation_id_status_event_kind '
        'ON event_callback (conversation_id, status, event_kind);\n\n'
    )
    assert 'CONCURRENTLY' not in sql


@pytest.mark.parametrize('revision', ['011', '012', '013', '014', '015'])
def test_later_migrations_compile_for_postgresql(revision):
    sql = _compile_postgresql_upgrade(revision)

    assert sql
    assert 'CONCURRENTLY' not in sql
