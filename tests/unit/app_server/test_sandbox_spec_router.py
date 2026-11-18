from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from openhands.app_server.config_api.config_models import AppMode
from openhands.app_server.sandbox.sandbox_spec_router import (
    _can_manage_execution_profiles,
    _require_execution_profile_admin,
)


@pytest.mark.parametrize(
    ('app_mode', 'user_id', 'admin_ids', 'expected'),
    [
        (AppMode.OPENHANDS, None, [], True),
        (AppMode.SAAS, 'admin-user', ['admin-user'], True),
        (AppMode.SAAS, 'ordinary-user', ['admin-user'], False),
        (AppMode.SAAS, None, [], False),
    ],
)
async def test_execution_profile_admin_policy(app_mode, user_id, admin_ids, expected):
    user_context = AsyncMock()
    user_context.get_user_id.return_value = user_id
    config = SimpleNamespace(
        app_mode=app_mode,
        execution_profile_admin_user_ids=admin_ids,
    )
    with patch(
        'openhands.app_server.sandbox.sandbox_spec_router.get_global_config',
        return_value=config,
    ):
        assert await _can_manage_execution_profiles(user_context) is expected


async def test_execution_profile_admin_dependency_returns_403():
    user_context = AsyncMock()
    user_context.get_user_id.return_value = 'ordinary-user'
    config = SimpleNamespace(
        app_mode=AppMode.SAAS,
        execution_profile_admin_user_ids=['admin-user'],
    )
    with patch(
        'openhands.app_server.sandbox.sandbox_spec_router.get_global_config',
        return_value=config,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _require_execution_profile_admin(user_context)

    assert exc_info.value.status_code == 403
