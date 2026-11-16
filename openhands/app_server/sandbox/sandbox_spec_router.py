"""Runtime Images router for OpenHands App Server."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from openhands.app_server.config import (
    depends_sandbox_spec_service,
    depends_user_context,
    get_global_config,
)
from openhands.app_server.config_api.config_models import AppMode
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxSpecAdminCapabilities,
    SandboxSpecCreate,
    SandboxSpecInfo,
    SandboxSpecInfoPage,
    SandboxSpecUpdate,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
)
from openhands.app_server.user.user_context import UserContext
from openhands.app_server.utils.dependencies import get_dependencies

# We use the get_dependencies method here to signal to the OpenAPI docs that this endpoint
# is protected. The actual protection is provided by SetAuthCookieMiddleware
# Sandboxes specs share a single immutable list for the server right now, but that is likely to
# change in the future
router = APIRouter(
    prefix='/sandbox-specs', tags=['Sandbox'], dependencies=get_dependencies()
)
sandbox_spec_service_dependency = depends_sandbox_spec_service()
user_context_dependency = depends_user_context()


async def _can_manage_execution_profiles(user_context: UserContext) -> bool:
    config = get_global_config()
    user_id = await user_context.get_user_id()
    if config.app_mode == AppMode.OPENHANDS and user_id is None:
        return True
    return user_id is not None and user_id in config.execution_profile_admin_user_ids


async def _require_execution_profile_admin(
    user_context: UserContext = user_context_dependency,
) -> None:
    if not await _can_manage_execution_profiles(user_context):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Execution profile administrator access required',
        )


# Read methods


@router.get('/search')
async def search_sandbox_specs(
    page_id: Annotated[
        str | None,
        Query(title='Optional next_page_id from the previously returned page'),
    ] = None,
    limit: Annotated[
        int,
        Query(title='The max number of results in the page', gt=0, le=100),
    ] = 100,
    sandbox_spec_service: SandboxSpecService = sandbox_spec_service_dependency,
) -> SandboxSpecInfoPage:
    """Search / List sandbox specs."""
    return await sandbox_spec_service.search_sandbox_specs(page_id=page_id, limit=limit)


@router.get('')
async def batch_get_sandbox_specs(
    id: Annotated[list[str], Query()],
    sandbox_spec_service: SandboxSpecService = sandbox_spec_service_dependency,
) -> list[SandboxSpecInfo | None]:
    """Get a batch of sandbox specs given their ids, returning null for any missing."""
    if len(id) > 100:
        raise HTTPException(
            status_code=400,
            detail=f'Cannot request more than 100 sandbox specs at once, got {len(id)}',
        )
    sandbox_specs = await sandbox_spec_service.batch_get_sandbox_specs(id)
    return sandbox_specs


@router.get('/admin/capabilities')
async def get_sandbox_spec_admin_capabilities(
    user_context: UserContext = user_context_dependency,
) -> SandboxSpecAdminCapabilities:
    return SandboxSpecAdminCapabilities(
        can_manage=await _can_manage_execution_profiles(user_context)
    )


@router.get('/admin/search')
async def search_all_sandbox_specs(
    page_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=100)] = 100,
    _: None = Depends(_require_execution_profile_admin),
    sandbox_spec_service: SandboxSpecService = sandbox_spec_service_dependency,
) -> SandboxSpecInfoPage:
    return await sandbox_spec_service.search_all_sandbox_specs(page_id, limit)


@router.post('/admin', status_code=status.HTTP_201_CREATED)
async def create_sandbox_spec(
    request: SandboxSpecCreate,
    _: None = Depends(_require_execution_profile_admin),
    sandbox_spec_service: SandboxSpecService = sandbox_spec_service_dependency,
) -> SandboxSpecInfo:
    try:
        return await sandbox_spec_service.create_sandbox_spec(request)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put('/admin/{sandbox_spec_id}')
async def update_sandbox_spec(
    sandbox_spec_id: str,
    request: SandboxSpecUpdate,
    _: None = Depends(_require_execution_profile_admin),
    sandbox_spec_service: SandboxSpecService = sandbox_spec_service_dependency,
) -> SandboxSpecInfo:
    try:
        result = await sandbox_spec_service.update_sandbox_spec(
            sandbox_spec_id, request
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return result


@router.delete('/admin/{sandbox_spec_id}')
async def delete_sandbox_spec(
    sandbox_spec_id: str,
    _: None = Depends(_require_execution_profile_admin),
    sandbox_spec_service: SandboxSpecService = sandbox_spec_service_dependency,
) -> None:
    try:
        deleted = await sandbox_spec_service.delete_sandbox_spec(sandbox_spec_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
