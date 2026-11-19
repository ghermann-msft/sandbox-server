from unittest.mock import AsyncMock

import pytest

from openhands.app_server.sandbox.sandbox_lifecycle import (
    release_sandbox_if_unreferenced,
)


@pytest.mark.asyncio
async def test_release_sandbox_if_unreferenced_deletes_sandbox():
    info_service = AsyncMock()
    info_service.count_conversations_by_sandbox_id.return_value = 0
    sandbox_service = AsyncMock()
    sandbox_service.delete_sandbox.return_value = True

    released = await release_sandbox_if_unreferenced(
        info_service, sandbox_service, 'sandbox-1'
    )

    assert released is True
    sandbox_service.delete_sandbox.assert_awaited_once_with('sandbox-1')


@pytest.mark.asyncio
async def test_release_sandbox_if_unreferenced_keeps_referenced_sandbox():
    info_service = AsyncMock()
    info_service.count_conversations_by_sandbox_id.return_value = 1
    sandbox_service = AsyncMock()

    released = await release_sandbox_if_unreferenced(
        info_service, sandbox_service, 'sandbox-1'
    )

    assert released is False
    sandbox_service.delete_sandbox.assert_not_awaited()
