from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from openhands.app_server.app_conversation.app_conversation_models import (
    ORGANIZATION_ID_TAG_KEY,
    AppConversationInfo,
    AppConversationStartRequest,
)
from openhands.app_server.user_auth.organization_models import (
    OrganizationInfo,
    OrganizationList,
    OrganizationMembership,
)
from openhands.app_server.user_auth.organization_service import (
    bind_conversation_organization,
)


def _user_auth() -> AsyncMock:
    user_auth = AsyncMock()
    user_auth.get_user_id.return_value = 'user-1'
    user_auth.get_organizations.return_value = OrganizationList(
        items=[
            OrganizationInfo(
                id='personal-1',
                name='Personal',
                is_personal=True,
            ),
            OrganizationInfo(id='halo-studios', name='Halo Studios'),
        ],
        current_org_id='halo-studios',
    )
    user_auth.get_organization_membership.return_value = OrganizationMembership(
        org_id='halo-studios',
        user_id='user-1',
        role='member',
        permissions={'hindsight:recall', 'hindsight:retain'},
    )
    return user_auth


@pytest.mark.asyncio
async def test_canvas_organization_contract_uses_user_auth_rbac():
    user_auth = _user_auth()
    organizations = await user_auth.get_organizations()
    membership = await user_auth.get_organization_membership('halo-studios')

    assert organizations.current_org_id == 'halo-studios'
    assert organizations.items[0].is_personal is True
    assert membership.model_dump() == {
        'org_id': 'halo-studios',
        'user_id': 'user-1',
        'role': 'member',
        'permissions': {'hindsight:recall', 'hindsight:retain'},
    }


@pytest.mark.asyncio
async def test_conversation_organization_requires_matching_membership():
    user_auth = _user_auth()
    request = AppConversationStartRequest()

    await bind_conversation_organization(request, 'halo-studios', user_auth)

    assert request.organization_id == 'halo-studios'
    info = AppConversationInfo(
        created_by_user_id='user-1',
        sandbox_id='sandbox-1',
        tags={ORGANIZATION_ID_TAG_KEY: request.organization_id},
    )
    assert info.organization_id == 'halo-studios'

    user_auth.get_organization_membership.return_value = None
    with pytest.raises(HTTPException) as exc_info:
        await bind_conversation_organization(
            AppConversationStartRequest(), 'forged-org', user_auth
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_conversation_body_cannot_self_select_organization():
    user_auth = _user_auth()

    with pytest.raises(HTTPException) as exc_info:
        await bind_conversation_organization(
            AppConversationStartRequest(organization_id='halo-studios'),
            None,
            user_auth,
        )
    assert exc_info.value.status_code == 403

    with pytest.raises(HTTPException) as exc_info:
        await bind_conversation_organization(
            AppConversationStartRequest(organization_id='personal-1'),
            'halo-studios',
            user_auth,
        )
    assert exc_info.value.status_code == 400
