import sys
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, HTTPException, status

from openhands.app_server.app_conversation.app_conversation_info_service import (
    AppConversationInfoService,
)
from openhands.app_server.config import (
    depends_app_conversation_info_service,
    depends_httpx_client,
    depends_jwt_service,
    depends_sandbox_service,
    depends_user_context,
    get_app_conversation_info_service,
    get_global_config,
)
from openhands.app_server.hindsight.hindsight_config import (
    load_hindsight_scope_bindings,
)
from openhands.app_server.hindsight.hindsight_models import (
    HindsightAuthorizationContext,
    HindsightReadRequest,
    HindsightRecallRequest,
    HindsightRecallResponse,
    HindsightReflectResponse,
    HindsightRetainApprovalRequest,
    HindsightRetainApprovalResponse,
    HindsightRetainRequest,
    HindsightRetainResponse,
    HindsightScopeBindings,
)
from openhands.app_server.hindsight.hindsight_service import (
    HindsightBroker,
    HindsightBrokerSettings,
    HindsightOutboxStatus,
    authorize_conversation_organization,
    authorize_scope_operation,
    content_sha256,
    derive_bank_id,
    derive_event_id,
    resolve_scope,
    retain_requires_approval,
    validate_retain_approval,
)
from openhands.app_server.organization.organization_models import (
    OrganizationPermission,
)
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.sandbox.sandbox_models import SandboxInfo
from openhands.app_server.sandbox.sandbox_service import SandboxService
from openhands.app_server.sandbox.session_auth import validate_session_key
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.user.auth_user_context import AuthUserContext
from openhands.app_server.user.specifiy_user_context import ADMIN, USER_CONTEXT_ATTR
from openhands.app_server.user.user_context import UserContext
from openhands.app_server.user_auth.user_auth import get_for_user

router = APIRouter(prefix='/hindsight', tags=['Hindsight'])


async def _broker(http_client: httpx.AsyncClient | None = None) -> HindsightBroker:
    config = get_global_config()
    if (
        not config.hindsight_enabled
        or (config.hindsight_linux_only and not sys.platform.startswith('linux'))
        or not config.hindsight_base_url
        or not config.hindsight_api_key_file
        or not config.hindsight_bank_secret_file
    ):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Hindsight broker is not configured',
        )
    settings = HindsightBrokerSettings(
        base_url=config.hindsight_base_url,
        api_key_file=config.hindsight_api_key_file,
        bank_secret_file=config.hindsight_bank_secret_file,
        fail_open=config.hindsight_fail_open,
        max_outbox_pending=config.hindsight_max_outbox_pending,
        max_attempts=config.hindsight_max_attempts,
        request_timeout_seconds=config.hindsight_request_timeout_seconds,
    )
    try:
        settings.get_api_key()
        settings.get_bank_secret()
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Hindsight broker secret files are unavailable',
        ) from exc
    return HindsightBroker(
        settings=settings,
        async_session_maker=await config.db_session.get_async_session_maker(),
        http_client=http_client,
    )


async def _authorization_for_user(
    user_id: str | None,
    conversation,
) -> HindsightAuthorizationContext:
    organization_id = conversation.organization_id
    if not user_id or organization_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Conversation has no trusted RBAC authority',
        )
    try:
        user_auth = await get_for_user(user_id)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='OpenHands RBAC authority is unavailable',
        ) from exc
    user_context = AuthUserContext(user_auth=user_auth)
    identity = await user_context.get_authentication_identity()
    if identity is None or identity.tenant_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Authenticated immutable identity is required',
        )
    try:
        session_maker = await get_global_config().db_session.get_async_session_maker()
        async with session_maker() as db_session:
            membership = await OrganizationService(
                db_session, user_context
            ).require_permission(
                organization_id,
                OrganizationPermission.ORG_VIEW,
            )
        authorization = HindsightAuthorizationContext(
            tenant_id=UUID(identity.tenant_id),
            object_id=UUID(identity.subject_id),
            organization_id=str(organization_id),
            role=membership.role.value,
            permissions={permission.value for permission in membership.permissions},
        )
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='OpenHands RBAC returned invalid immutable identity',
        ) from exc
    return authorization


async def _authorization_for_identity(
    identity_token: str,
    conversation,
    jwt_service: JwtService,
) -> HindsightAuthorizationContext:
    try:
        claims = jwt_service.verify_jws_token(identity_token)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Invalid Hindsight identity',
        ) from exc
    if claims.get('purpose') != 'hindsight_memory':
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Invalid Hindsight identity purpose',
        )
    user_id = claims.get('user_id')
    if (
        not isinstance(user_id, str)
        or claims.get('sandbox_id') != conversation.sandbox_id
        or claims.get('organization_id') != str(conversation.organization_id)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Hindsight identity does not match conversation authority',
        )
    authorization = await _authorization_for_user(user_id, conversation)
    if claims.get('tenant_id') != str(authorization.tenant_id) or claims.get(
        'object_id'
    ) != str(authorization.object_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Hindsight identity no longer matches authenticated identity',
        )
    return authorization


def _scope_bindings() -> HindsightScopeBindings:
    file_path = get_global_config().hindsight_scope_bindings_json_file
    if file_path is None:
        return HindsightScopeBindings()
    try:
        return load_hindsight_scope_bindings(file_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Trusted Hindsight scope bindings are unavailable',
        ) from exc


async def _conversation(conversation_id):
    state = InjectorState()
    setattr(state, USER_CONTEXT_ATTR, ADMIN)
    async with get_app_conversation_info_service(state) as service:
        conversation = await service.get_app_conversation_info(conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Conversation not found')
    return conversation


async def _conversation_for_sandbox(conversation_id, sandbox: SandboxInfo):
    conversation = await _conversation(conversation_id)
    if conversation.sandbox_id != sandbox.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Conversation is not bound to this sandbox',
        )
    return conversation


async def _authorized_read(
    request: HindsightReadRequest,
    http_client: httpx.AsyncClient,
    jwt_service: JwtService,
):
    conversation = await _conversation(request.conversation_id)
    authorization = await _authorization_for_identity(
        request.identity, conversation, jwt_service
    )
    principal = authorization.principal
    broker = await _broker(http_client)
    bindings = _scope_bindings()
    resolved = [
        resolve_scope(
            scope,
            principal,
            conversation.sandbox_id,
            conversation.selected_repository,
            conversation.git_provider.value if conversation.git_provider else None,
            get_global_config().hindsight_deployment_id,
            bindings,
        )
        for scope in request.scope
    ]
    authorize_conversation_organization(
        conversation.organization_id, authorization, resolved
    )
    for scope in resolved:
        authorize_scope_operation(authorization, scope, 'hindsight:recall')
    if len({(scope.type, scope.id) for scope in resolved}) != len(resolved):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Duplicate memory scopes are not allowed',
        )
    banks = [
        (
            derive_bank_id(principal, scope, broker.settings.get_bank_secret()),
            scope,
        )
        for scope in resolved
    ]
    return broker, banks


@router.post('/recall', response_model=HindsightRecallResponse)
async def recall(
    request: HindsightRecallRequest,
    http_client: Annotated[httpx.AsyncClient, depends_httpx_client()],
    jwt_service: Annotated[JwtService, depends_jwt_service()],
):
    broker, banks = await _authorized_read(request, http_client, jwt_service)
    available, memories = await broker.recall(
        request.query, banks, 10, request.max_tokens
    )
    return HindsightRecallResponse(available=available, memories=memories)


@router.post('/reflect', response_model=HindsightReflectResponse)
async def reflect(
    request: HindsightReadRequest,
    http_client: Annotated[httpx.AsyncClient, depends_httpx_client()],
    jwt_service: Annotated[JwtService, depends_jwt_service()],
):
    broker, banks = await _authorized_read(request, http_client, jwt_service)
    available, text, sources = await broker.reflect(
        request.query, banks, request.max_tokens
    )
    return HindsightReflectResponse(available=available, content=text, memories=sources)


@router.post(
    '/retain/approvals',
    response_model=HindsightRetainApprovalResponse,
)
async def approve_retain(
    request: HindsightRetainApprovalRequest,
    user_context: Annotated[UserContext, depends_user_context()],
    conversation_service: Annotated[
        AppConversationInfoService, depends_app_conversation_info_service()
    ],
    sandbox_service: Annotated[SandboxService, depends_sandbox_service()],
    jwt_service: Annotated[JwtService, depends_jwt_service()],
):
    user_id = await user_context.get_user_id()
    conversation = await conversation_service.get_app_conversation_info(
        request.conversation_id
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Conversation not found')
    authorization = await _authorization_for_user(user_id, conversation)
    principal = authorization.principal
    sandbox = await sandbox_service.get_sandbox(conversation.sandbox_id)
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Sandbox not found')
    if sandbox.created_by_user_id != user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail='Sandbox is owned by another user'
        )
    scope = resolve_scope(
        request.scope,
        principal,
        sandbox.id,
        conversation.selected_repository,
        conversation.git_provider.value if conversation.git_provider else None,
        get_global_config().hindsight_deployment_id,
        _scope_bindings(),
    )
    authorize_conversation_organization(
        (
            str(conversation.organization_id)
            if conversation.organization_id is not None
            else None
        ),
        authorization,
        [scope],
    )
    authorize_scope_operation(authorization, scope, 'hindsight:retain')
    payload = {
        'purpose': 'hindsight_retain_approval',
        'user_id': user_id,
        'tenant_id': str(principal.tenant_id),
        'object_id': str(principal.object_id),
        'conversation_id': str(request.conversation_id),
        'sandbox_id': sandbox.id,
        'source_event_id': request.source_event_id,
        'scope': scope.model_dump(mode='json'),
        'content_sha256': request.content_sha256,
    }
    return HindsightRetainApprovalResponse(
        approval_token=jwt_service.create_jws_token(
            payload, expires_in=timedelta(minutes=10)
        )
    )


@router.post(
    '/retain',
    response_model=HindsightRetainResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retain(
    request: HindsightRetainRequest,
    jwt_service: Annotated[JwtService, depends_jwt_service()],
    session_api_key: Annotated[str | None, Header(alias='X-Session-API-Key')] = None,
):
    sandbox = await validate_session_key(session_api_key)
    conversation = await _conversation_for_sandbox(request.conversation_id, sandbox)
    owner_user_id = sandbox.created_by_user_id
    authorization = await _authorization_for_user(owner_user_id, conversation)
    principal = authorization.principal
    assert owner_user_id is not None
    scope = resolve_scope(
        request.scope,
        principal,
        sandbox.id,
        conversation.selected_repository,
        conversation.git_provider.value if conversation.git_provider else None,
        get_global_config().hindsight_deployment_id,
        _scope_bindings(),
    )
    authorize_conversation_organization(
        conversation.organization_id, authorization, [scope]
    )
    authorize_scope_operation(authorization, scope, 'hindsight:retain')
    digest = content_sha256(request.content)
    if retain_requires_approval(scope):
        if request.approval_token is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail='Retain approval is required for this scope',
            )
        try:
            approval = jwt_service.verify_jws_token(request.approval_token)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail='Invalid retain approval'
            ) from exc
        validate_retain_approval(
            approval,
            user_id=owner_user_id,
            principal=principal,
            conversation_id=str(request.conversation_id),
            sandbox_id=sandbox.id,
            source_event_id=request.source_event_id,
            scope=scope,
            content_digest=digest,
        )
    broker = await _broker()
    event_id = derive_event_id(
        principal,
        str(request.conversation_id),
        request.source_event_id,
        scope,
        digest,
    )
    bank_id = derive_bank_id(principal, scope, broker.settings.get_bank_secret())
    payload = {
        'content': request.content,
        'timestamp': request.occurred_at.isoformat(),
        'context': 'OpenHands approved durable memory',
        'document_id': event_id,
        'metadata': {
            'source_event_id': request.source_event_id,
            'source_conversation_id': str(request.conversation_id),
            'scope': scope.model_dump(mode='json'),
            'event_id': event_id,
        },
    }
    row, duplicate = await broker.enqueue(event_id, bank_id, payload)
    response_status: Literal['queued', 'delivered', 'dead_letter']
    if row.status == HindsightOutboxStatus.DELIVERED:
        response_status = 'delivered'
    elif row.status == HindsightOutboxStatus.DEAD_LETTER:
        response_status = 'dead_letter'
    else:
        response_status = 'queued'
    return HindsightRetainResponse(
        event_id=event_id,
        status=response_status,
        duplicate=duplicate,
    )
