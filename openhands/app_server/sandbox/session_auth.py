"""Shared session-key authentication for sandbox-scoped endpoints.

Both the sandbox router and the user router need to validate
``X-Session-API-Key`` headers.  This module centralises that logic so
it lives in exactly one place.

The ``InjectorState`` + ``ADMIN`` pattern used here is established in
``webhook_router.py`` — the sandbox service requires an admin context to
look up sandboxes across all users by session key, but the session key
itself acts as the proof of access.

Security Note:
    Session API keys are only valid while the sandbox is RUNNING. This prevents
    leaked keys from being used to access secrets after a sandbox has been
    paused, stopped, or deleted. See validate_session_key() for enforcement.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    APIKeyHeader,
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from openhands.app_server.config import get_global_config, get_sandbox_service
from openhands.app_server.config_api.config_models import AppMode
from openhands.app_server.sandbox.sandbox_models import SandboxInfo, SandboxStatus
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.user.auth_user_context import (
    USER_AUTH_ATTR,
    AuthUserContext,
    AuthUserContextInjector,
)
from openhands.app_server.user.specifiy_user_context import (
    ADMIN,
    SANDBOX_USER_CONTEXT_ATTR,
    USER_CONTEXT_ATTR,
    SandboxUserContext,
)
from openhands.app_server.user_auth.user_auth import get_user_auth
from openhands.app_server.utils.dependencies import (
    _SESSION_API_KEY as _STATIC_SESSION_API_KEY,
)
from openhands.app_server.utils.dependencies import check_session_api_key

if TYPE_CHECKING:
    from openhands.app_server.user.user_context import UserContext

_logger = logging.getLogger(__name__)
_APP_API_BEARER = HTTPBearer(auto_error=False)
_STATIC_SESSION_API_KEY_HEADER = APIKeyHeader(
    name='X-Session-API-Key', auto_error=False
)


async def validate_session_key(session_api_key: str | None) -> SandboxInfo:
    """Validate an ``X-Session-API-Key`` and return the associated sandbox.

    Security:
        This function enforces that session API keys are only valid for RUNNING
        sandboxes. This is a critical security measure to prevent leaked keys
        from being used to access user secrets after a sandbox has been paused,
        stopped, or deleted.

    Raises:
        HTTPException(401): if the key is missing or does not map to a sandbox.
        HTTPException(401): if the sandbox is not in RUNNING state.
        HTTPException(401): in SAAS mode if the sandbox has no owning user.
    """
    if not session_api_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail='X-Session-API-Key header is required',
        )

    # The sandbox service is scoped to users. To look up a sandbox by session
    # key (which could belong to *any* user) we need an admin context.  This
    # is the same pattern used in webhook_router.valid_sandbox().
    state = InjectorState()
    setattr(state, USER_CONTEXT_ATTR, ADMIN)

    async with get_sandbox_service(state) as sandbox_service:
        sandbox_info = await sandbox_service.get_sandbox_by_session_api_key(
            session_api_key
        )

    if sandbox_info is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail='Invalid session API key'
        )

    # Security: Reject session keys for non-running sandboxes.
    # This prevents leaked keys from being used to access secrets after
    # the sandbox has been paused, stopped, or deleted.
    if sandbox_info.status != SandboxStatus.RUNNING:
        _logger.warning(
            'Session key rejected for non-running sandbox',
            extra={
                'sandbox_id': sandbox_info.id,
                'status': sandbox_info.status.value,
            },
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail='Sandbox is not running',
        )

    if not sandbox_info.created_by_user_id:
        if get_global_config().app_mode == AppMode.SAAS:
            _logger.error(
                'Sandbox had no user specified',
                extra={'sandbox_id': sandbox_info.id},
            )
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail='Sandbox had no user specified',
            )

    return sandbox_info


async def validate_session_key_ownership(
    user_context: UserContext,
    session_api_key: str | None,
) -> None:
    """Validate session key and verify it belongs to a sandbox owned by the caller.

    This combines session key validation with ownership verification, ensuring
    the session key is valid AND belongs to a sandbox owned by the authenticated user.

    Args:
        user_context: The authenticated user's context.
        session_api_key: The session API key to validate.

    Raises:
        HTTPException(401): if the key is missing, invalid, or user cannot be determined.
        HTTPException(403): if the sandbox is owned by a different user.
    """
    sandbox_info = await validate_session_key(session_api_key)

    # Verify the sandbox is owned by the authenticated user.
    caller_id = await user_context.get_user_id()
    if not caller_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail='Cannot determine authenticated user',
        )

    if sandbox_info.created_by_user_id != caller_id:
        _logger.warning(
            'Session key user mismatch: sandbox owner=%s, caller=%s',
            sandbox_info.created_by_user_id,
            caller_id,
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Session API key does not belong to the authenticated user',
        )


async def authenticate_app_conversation_request(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_APP_API_BEARER),
    static_session_api_key: str | None = Depends(_STATIC_SESSION_API_KEY_HEADER),
) -> None:
    """Authenticate app-conversation routes without changing global auth.

    A bearer credential that maps to a running sandbox acts as the sandbox
    owner's app API credential. Other bearer credentials continue through the
    existing Cloud/Entra user-auth path. When the deployment configures the
    namespace-static ``SESSION_API_KEY``, requests that are not authenticated by
    a sandbox bearer must still provide that key.
    """
    if bearer is not None:
        try:
            sandbox_info = await validate_session_key(bearer.credentials)
        except HTTPException as exc:
            if not (
                exc.status_code == status.HTTP_401_UNAUTHORIZED
                and exc.detail == 'Invalid session API key'
            ):
                raise
        else:
            owner_id = sandbox_info.created_by_user_id
            if not owner_id:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    detail='Sandbox has no associated user',
                )

            user_auth = await get_user_auth(request)
            user_injector = get_global_config().user
            managed_llm = (
                user_injector.managed_llm
                if isinstance(user_injector, AuthUserContextInjector)
                else None
            )
            setattr(request.state, USER_AUTH_ATTR, user_auth)
            setattr(
                request.state,
                SANDBOX_USER_CONTEXT_ATTR,
                SandboxUserContext(user_id=owner_id, sandbox_id=sandbox_info.id),
            )
            setattr(
                request.state,
                USER_CONTEXT_ATTR,
                AuthUserContext(
                    user_auth=user_auth,
                    managed_llm=managed_llm,
                    trusted_user_id=owner_id,
                ),
            )
            return

    if _STATIC_SESSION_API_KEY:
        check_session_api_key(static_session_api_key)
