from fastapi import HTTPException, status

from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversationStartRequest,
)
from openhands.app_server.user_auth.organization_models import OrganizationMembership
from openhands.app_server.user_auth.user_auth import UserAuth


async def bind_conversation_organization(
    start_request: AppConversationStartRequest,
    organization_id: str | None,
    user_auth: UserAuth,
) -> None:
    if organization_id is None:
        if start_request.organization_id is not None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail='X-Org-Id is required to select an organization',
            )
        return

    try:
        value = await user_auth.get_organization_membership(organization_id)
    except NotImplementedError as exc:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail='The configured UserAuth does not provide organization RBAC',
        ) from exc

    membership = None if value is None else OrganizationMembership.model_validate(value)
    user_id = await user_auth.get_user_id()
    if (
        membership is None
        or membership.org_id != organization_id
        or user_id is None
        or membership.user_id != user_id
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Organization membership is required',
        )
    if (
        start_request.organization_id is not None
        and start_request.organization_id != organization_id
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail='Request organization does not match X-Org-Id',
        )
    start_request.organization_id = organization_id
