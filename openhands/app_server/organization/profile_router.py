import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Header, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.config import depends_jwt_service, depends_user_context
from openhands.app_server.organization.organization_models import (
    OrganizationPermission,
)
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.organization.profile_models import (
    OrganizationProfileActivateResponse,
    OrganizationProfileDetailResponse,
    OrganizationProfileInfo,
    OrganizationProfileListResponse,
    OrganizationProfileMutationResponse,
    OrganizationProfileRenameRequest,
    OrganizationProfileSaveRequest,
)
from openhands.app_server.organization.profile_service import (
    OrganizationProfileService,
    ProfileAlreadyExistsError,
    ProfileLimitExceededError,
    ProfileNotFoundError,
)
from openhands.app_server.services.db_session import depends_db_session
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.settings.llm_profiles import has_real_api_key
from openhands.app_server.user.user_context import UserContext

router = APIRouter(
    prefix='/api/organizations/{organization_id}/profiles',
    tags=['organization-profiles'],
)
db_session_dependency = depends_db_session()
user_context_dependency = depends_user_context()
jwt_service_dependency = depends_jwt_service()
requested_org_id_header = Header(default=None, alias='X-OpenHands-Organization-ID')
_MANAGED_PROXY_URL = os.environ.get(
    'LITE_LLM_API_URL', 'https://llm-proxy.app.all-hands.dev'
)
ProfileName = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$',
    ),
]


async def _service(
    organization_id: UUID,
    requested_org_id: UUID | None,
    db_session: AsyncSession,
    user_context: UserContext,
    jwt_service: JwtService,
) -> OrganizationProfileService:
    selected = await OrganizationService(
        db_session, user_context
    ).resolve_organization_id(requested_org_id, OrganizationPermission.ORG_VIEW)
    if selected != organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Selected organization does not match requested organization',
        )
    return OrganizationProfileService(db_session, user_context, jwt_service)


@router.get('')
async def list_profiles(
    organization_id: UUID,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> OrganizationProfileListResponse:
    profiles = await (
        await _service(
            organization_id,
            requested_org_id,
            db_session,
            user_context,
            jwt_service,
        )
    ).load(organization_id)
    return OrganizationProfileListResponse(
        profiles=[
            OrganizationProfileInfo(**summary)
            for summary in profiles.summaries(managed_proxy_url=_MANAGED_PROXY_URL)
        ],
        active_profile=profiles.active,
    )


@router.get('/{name}')
async def get_profile(
    organization_id: UUID,
    name: ProfileName,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> OrganizationProfileDetailResponse:
    profiles = await (
        await _service(
            organization_id,
            requested_org_id,
            db_session,
            user_context,
            jwt_service,
        )
    ).load(organization_id)
    profile = profiles.get(name)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{name}' not found",
        )
    config = profile.model_dump(mode='json')
    config['api_key'] = None
    return OrganizationProfileDetailResponse(
        name=name,
        config=config,
        api_key_set=has_real_api_key(profile.api_key),
    )


@router.post('/{name}', status_code=status.HTTP_201_CREATED)
async def save_profile(
    organization_id: UUID,
    name: ProfileName,
    request: Annotated[OrganizationProfileSaveRequest, Body()],
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> OrganizationProfileMutationResponse:
    try:
        await (
            await _service(
                organization_id,
                requested_org_id,
                db_session,
                user_context,
                jwt_service,
            )
        ).save(organization_id, name, request)
    except ProfileLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return OrganizationProfileMutationResponse(
        name=name, message=f"Profile '{name}' saved"
    )


@router.delete('/{name}')
async def delete_profile(
    organization_id: UUID,
    name: ProfileName,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> OrganizationProfileMutationResponse:
    await (
        await _service(
            organization_id,
            requested_org_id,
            db_session,
            user_context,
            jwt_service,
        )
    ).delete(organization_id, name)
    return OrganizationProfileMutationResponse(
        name=name, message=f"Profile '{name}' deleted"
    )


@router.post('/{name}/activate')
async def activate_profile(
    organization_id: UUID,
    name: ProfileName,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> OrganizationProfileActivateResponse:
    try:
        profiles = await (
            await _service(
                organization_id,
                requested_org_id,
                db_session,
                user_context,
                jwt_service,
            )
        ).activate(organization_id, name)
    except ProfileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return OrganizationProfileActivateResponse(
        name=name,
        message=f"Switched to profile '{name}'",
        model=profiles.require(name).model,
    )


@router.post('/{name}/rename')
async def rename_profile(
    organization_id: UUID,
    name: ProfileName,
    request: OrganizationProfileRenameRequest,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> OrganizationProfileMutationResponse:
    try:
        await (
            await _service(
                organization_id,
                requested_org_id,
                db_session,
                user_context,
                jwt_service,
            )
        ).rename(organization_id, name, request.new_name)
    except ProfileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ProfileAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return OrganizationProfileMutationResponse(
        name=request.new_name,
        message=f"Profile '{name}' renamed to '{request.new_name}'",
    )
