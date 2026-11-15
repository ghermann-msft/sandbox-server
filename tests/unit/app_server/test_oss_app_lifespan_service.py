from unittest.mock import patch

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

    with (
        patch(
            'openhands.app_server.app_lifespan.oss_app_lifespan_service.command.upgrade'
        ) as upgrade,
        patch('openhands.app_server.app_lifespan.oss_app_lifespan_service.os.chdir'),
    ):
        service.run_alembic()

    alembic_config, revision = upgrade.call_args.args
    assert revision == 'head'
    assert alembic_config.get_main_option('script_location').endswith('alembic')
