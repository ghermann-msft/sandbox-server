from contextlib import nullcontext
from unittest.mock import MagicMock, call, patch

import pytest

from openhands.app_server.app_lifespan.oss_app_lifespan_service import (
    OssAppLifespanService,
)


@pytest.mark.asyncio
async def test_startup_runs_alembic_before_entering_lifespan():
    service = OssAppLifespanService()

    with patch.object(OssAppLifespanService, 'run_alembic') as run_alembic:
        result = await service.__aenter__()

    assert result is service
    run_alembic.assert_called_once_with()


def test_alembic_startup_upgrades_to_head():
    service = OssAppLifespanService()
    connection = MagicMock()
    connection.dialect.name = 'sqlite'
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)
    config = MagicMock()
    config.db_session.get_db_engine.return_value = engine

    with (
        patch(
            'openhands.app_server.app_lifespan.oss_app_lifespan_service.command.upgrade'
        ) as upgrade,
        patch('openhands.app_server.app_lifespan.oss_app_lifespan_service.os.chdir'),
        patch(
            'openhands.app_server.config.get_global_config',
            return_value=config,
        ),
    ):
        service.run_alembic()

    alembic_config, revision = upgrade.call_args.args
    assert revision == 'head'
    assert alembic_config.get_main_option('script_location').endswith('alembic')
    assert alembic_config.attributes == {}


def test_postgresql_alembic_startup_is_serialized():
    service = OssAppLifespanService()
    events = []
    connection = MagicMock()
    connection.dialect.name = 'postgresql'
    connection.in_transaction.return_value = False
    connection.execute.side_effect = lambda statement, params: events.append(
        (str(statement), params)
    )
    connection.commit.side_effect = lambda: events.append(('commit', None))
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)
    config = MagicMock()
    config.db_session.get_db_engine.return_value = engine

    with (
        patch(
            'openhands.app_server.app_lifespan.oss_app_lifespan_service.command.upgrade',
            side_effect=lambda *args: events.append(('upgrade', None)),
        ),
        patch('openhands.app_server.app_lifespan.oss_app_lifespan_service.os.chdir'),
        patch(
            'openhands.app_server.config.get_global_config',
            return_value=config,
        ),
    ):
        service.run_alembic()

    assert events == [
        (
            'SELECT pg_advisory_lock(:lock_id)',
            {'lock_id': 684_301_179_211_155_017},
        ),
        ('commit', None),
        ('upgrade', None),
        (
            'SELECT pg_advisory_unlock(:lock_id)',
            {'lock_id': 684_301_179_211_155_017},
        ),
        ('commit', None),
    ]
    assert connection.execute.call_count == 2
    assert connection.commit.call_args_list == [call(), call()]
