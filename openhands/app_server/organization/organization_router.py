from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.config import depends_user_context
from openhands.app_server.organization.organization_models import (
    OrganizationListResponse,
    OrganizationMembershipResponse,
)
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.services.db_session import depends_db_session
from openhands.app_server.user.user_context import UserContext

router = APIRouter(prefix='/api/organizations', tags=['organizations'])
requested_org_id_header = Header(default=None, alias='X-Org-Id')
db_session_dependency = depends_db_session()
user_context_dependency = depends_user_context()


@router.get('')
async def list_organizations(
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> OrganizationListResponse:
    return await OrganizationService(db_session, user_context).list_organizations(
        requested_org_id
    )


@router.get('/{organization_id}/me')
async def get_my_membership(
    organization_id: UUID,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> OrganizationMembershipResponse:
    if requested_org_id is not None and requested_org_id != organization_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='X-Org-Id does not match requested organization',
        )
    return await OrganizationService(db_session, user_context).get_membership(
        organization_id
    )
