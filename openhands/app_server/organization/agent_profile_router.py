from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Path, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.config import depends_user_context
from openhands.app_server.organization.agent_profile_models import (
    ActivateAgentProfileResponse,
    AgentProfileDetailResponse,
    AgentProfileInfo,
    AgentProfileListResponse,
    AgentProfileMutationResponse,
    RenameAgentProfileRequest,
)
from openhands.app_server.organization.agent_profile_service import (
    OrganizationAgentProfileService,
    ProfileLimitExceeded,
)
from openhands.app_server.organization.organization_models import OrganizationPermission
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.services.db_session import depends_db_session
from openhands.app_server.user.user_context import UserContext
from openhands.sdk.profiles import (
    AgentProfileDiagnostics,
    resolve_agent_profile_dry_run,
    safe_validation_error_detail,
    validate_agent_profile,
)
from openhands.sdk.profiles.agent_profile_store import PROFILE_NAME_PATTERN

router = APIRouter(prefix='/agent-profiles', tags=['agent-profiles'])
db_session_dependency = depends_db_session()
user_context_dependency = depends_user_context()
ProfileName = Annotated[
    str,
    Path(min_length=1, max_length=64, pattern=PROFILE_NAME_PATTERN),
]
ProfileId = Annotated[str, Path(min_length=1, max_length=128)]


async def _service(
    db_session: AsyncSession,
    user_context: UserContext,
) -> tuple[OrganizationAgentProfileService, UUID]:
    organization_id = await OrganizationService(
        db_session, user_context
    ).resolve_organization_id(permission=OrganizationPermission.ORG_VIEW)
    return OrganizationAgentProfileService(db_session, user_context), organization_id


@router.get('', response_model=AgentProfileListResponse)
async def list_agent_profiles(
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> AgentProfileListResponse:
    service, organization_id = await _service(db_session, user_context)
    profiles = await service.load(organization_id)
    return AgentProfileListResponse(
        profiles=[AgentProfileInfo(**summary) for summary in profiles.list_summaries()],
        active_agent_profile_id=profiles.active,
    )


@router.get('/{name}', response_model=AgentProfileDetailResponse)
async def get_agent_profile(
    name: ProfileName,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> AgentProfileDetailResponse:
    service, organization_id = await _service(db_session, user_context)
    profiles = await service.load(organization_id)
    try:
        profile = profiles.load(name)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent profile '{name}' not found",
        ) from exc
    return AgentProfileDetailResponse(
        name=name,
        profile=profile.model_dump(mode='json'),
    )


@router.post(
    '/{name}',
    response_model=AgentProfileMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def save_agent_profile(
    name: ProfileName,
    body: Annotated[dict[str, Any], Body()],
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> AgentProfileMutationResponse:
    try:
        profile = validate_agent_profile({**body, 'name': name})
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=safe_validation_error_detail(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Invalid agent profile',
        ) from exc

    service, organization_id = await _service(db_session, user_context)
    try:
        await service.save(organization_id, profile)
    except ProfileLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Agent profile limit reached (50).',
        ) from exc
    await db_session.commit()
    return AgentProfileMutationResponse(
        name=name, message=f"Agent profile '{name}' saved"
    )


@router.delete('/{name}', response_model=AgentProfileMutationResponse)
async def delete_agent_profile(
    name: ProfileName,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> AgentProfileMutationResponse:
    service, organization_id = await _service(db_session, user_context)
    await service.delete(organization_id, name)
    await db_session.commit()
    return AgentProfileMutationResponse(
        name=name, message=f"Agent profile '{name}' deleted"
    )


@router.post('/{name}/rename', response_model=AgentProfileMutationResponse)
async def rename_agent_profile(
    name: ProfileName,
    body: RenameAgentProfileRequest,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> AgentProfileMutationResponse:
    service, organization_id = await _service(db_session, user_context)
    try:
        await service.rename(organization_id, name, body.new_name)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent profile '{name}' not found",
        ) from exc
    except FileExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent profile '{body.new_name}' already exists",
        ) from exc
    await db_session.commit()
    return AgentProfileMutationResponse(
        name=body.new_name,
        message=f"Agent profile '{name}' renamed to '{body.new_name}'",
    )


@router.post('/{name}/materialize', response_model=AgentProfileDiagnostics)
async def materialize_agent_profile(
    name: ProfileName,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> AgentProfileDiagnostics:
    service, organization_id = await _service(db_session, user_context)
    profiles = await service.load(organization_id)
    try:
        profile = profiles.load(name)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent profile '{name}' not found",
        ) from exc
    user = await user_context.get_user_info()

    class _LLMProfileLoader:
        def load(self, profile_name: str, cipher: Any = None):
            del cipher
            llm = user.llm_profiles.get(profile_name)
            if llm is None:
                raise FileNotFoundError(profile_name)
            return llm

    return resolve_agent_profile_dry_run(
        profile,
        llm_store=_LLMProfileLoader(),
        mcp_config=user.agent_settings.mcp_config,
        available_skills=None,
    )


@router.post('/{profile_id}/activate', response_model=ActivateAgentProfileResponse)
async def activate_agent_profile(
    profile_id: ProfileId,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
) -> ActivateAgentProfileResponse:
    service, organization_id = await _service(db_session, user_context)
    try:
        await service.activate(organization_id, profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent profile with id '{profile_id}' not found",
        ) from exc
    await db_session.commit()
    return ActivateAgentProfileResponse(
        id=profile_id,
        message=f"Agent profile '{profile_id}' activated",
    )
