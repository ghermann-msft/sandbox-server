import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.app_conversation.app_conversation_info_service import (
    AppConversationInfoService,
)
from openhands.app_server.app_conversation.sql_app_conversation_info_service import (
    StoredConversationMetadata,
)
from openhands.app_server.sandbox.sandbox_service import SandboxService
from openhands.sdk import ConversationExecutionStatus

_logger = logging.getLogger(__name__)
_TERMINAL_EXECUTION_STATUSES = tuple(
    status.value for status in ConversationExecutionStatus if status.is_terminal()
)


async def active_sandbox_ids(db_session: AsyncSession) -> set[str]:
    result = await db_session.scalars(
        select(StoredConversationMetadata.sandbox_id)
        .distinct()
        .where(
            StoredConversationMetadata.conversation_version == 'V1',
            StoredConversationMetadata.sandbox_id.is_not(None),
            or_(
                StoredConversationMetadata.execution_status.is_(None),
                StoredConversationMetadata.execution_status.not_in(
                    _TERMINAL_EXECUTION_STATUSES
                ),
            ),
        )
    )
    return {sandbox_id for sandbox_id in result if sandbox_id is not None}


async def release_sandbox_if_inactive(
    app_conversation_info_service: AppConversationInfoService,
    sandbox_service: SandboxService,
    sandbox_id: str,
) -> bool:
    active_count = (
        await app_conversation_info_service.count_active_conversations_by_sandbox_id(
            sandbox_id
        )
    )
    if active_count:
        return False
    deleted = await sandbox_service.delete_sandbox(sandbox_id)
    if deleted:
        _logger.info('Released inactive sandbox %s', sandbox_id)
    return deleted
