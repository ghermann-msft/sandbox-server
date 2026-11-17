from fastapi import APIRouter, HTTPException, Request, status

from openhands.app_server.user_auth.organization_models import (
    OrganizationList,
    OrganizationMembership,
)
from openhands.app_server.user_auth.user_auth import get_user_auth
from openhands.app_server.utils.dependencies import get_dependencies

router = APIRouter(
    prefix='/api/organizations',
    tags=['Organizations'],
    dependencies=get_dependencies(),
)


@router.get('')
async def get_organizations(request: Request) -> OrganizationList:
    user_auth = await get_user_auth(request)
    try:
        organizations = await user_auth.get_organizations()
    except NotImplementedError as exc:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail='The configured UserAuth does not provide organization RBAC',
        ) from exc
    return OrganizationList.model_validate(organizations)


@router.get('/{organization_id}/me')
async def get_organization_membership(
    organization_id: str,
    request: Request,
) -> OrganizationMembership:
    user_auth = await get_user_auth(request)
    try:
        value = await user_auth.get_organization_membership(organization_id)
    except NotImplementedError as exc:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail='The configured UserAuth does not provide organization RBAC',
        ) from exc
    if value is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Organization membership is required',
        )
    return OrganizationMembership.model_validate(value)
