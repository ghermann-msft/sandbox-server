import importlib
from unittest.mock import MagicMock, call

from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.named_types import CreateEnumType


def test_event_callback_status_type_lifecycle(monkeypatch):
    migration = importlib.import_module(
        'openhands.app_server.app_lifespan.alembic.versions.002'
    )
    bind = MagicMock()
    operations = MagicMock()

    monkeypatch.setattr(migration.op, 'get_bind', MagicMock(return_value=bind))
    monkeypatch.setattr(migration.op, 'add_column', operations.add_column)
    monkeypatch.setattr(migration.op, 'drop_column', operations.drop_column)
    monkeypatch.setattr(migration.op, 'drop_index', MagicMock())
    monkeypatch.setattr(migration.op, 'create_index', MagicMock())
    monkeypatch.setattr(migration.op, 'f', lambda name: name)
    monkeypatch.setattr(
        migration.event_callback_status, 'create', operations.create_type
    )
    monkeypatch.setattr(
        migration.event_callback_status, 'drop', operations.drop_type
    )

    migration.upgrade()

    status_column = migration.op.add_column.call_args_list[0].args[1]
    assert operations.mock_calls[:2] == [
        call.create_type(bind, checkfirst=True),
        call.add_column('event_callback', status_column),
    ]
    assert status_column.name == 'status'
    assert status_column.type is migration.event_callback_status
    assert status_column.type.name == 'eventcallbackstatus'

    operations.reset_mock()
    migration.downgrade()

    assert operations.mock_calls[:2] == [
        call.drop_column('event_callback', 'status'),
        call.drop_type(bind, checkfirst=True),
    ]


def test_event_callback_status_postgresql_ddl():
    migration = importlib.import_module(
        'openhands.app_server.app_lifespan.alembic.versions.002'
    )

    ddl = str(
        CreateEnumType(migration.event_callback_status).compile(
            dialect=postgresql.dialect()
        )
    )

    assert ddl == (
        "CREATE TYPE eventcallbackstatus AS ENUM "
        "('ACTIVE', 'DISABLED', 'COMPLETED', 'ERROR')"
    )
