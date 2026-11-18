from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import cast
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, SecretStr
from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.agent_server.utils import utc_now
from openhands.app_server.config import depends_jwt_service
from openhands.app_server.integrations.github.github_service import GitHubService
from openhands.app_server.integrations.github.oauth_models import (
    StoredGitHubOAuthState,
)
from openhands.app_server.integrations.provider import ProviderToken
from openhands.app_server.integrations.service_types import ProviderType
from openhands.app_server.secrets.secrets_models import Secrets
from openhands.app_server.secrets.secrets_store import SecretsStore
from openhands.app_server.services.db_session import depends_db_session
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.user_auth import (
    get_secrets_store,
    get_user_id,
)
from openhands.app_server.utils.http_session import httpx_verify_option

_GITHUB_PROVIDER_ID = 1
_STATE_LIFETIME = timedelta(minutes=10)


class GitProviderStatus(BaseModel):
    id: int
    provider_type: str
    connected: bool
    needs_reconnect: bool


class GitProviderLinkResponse(BaseModel):
    start_url: str


def _github_oauth_config() -> tuple[str, str, str, str]:
    client_id = os.getenv('GITHUB_APP_CLIENT_ID', '').strip()
    client_secret = os.getenv('GITHUB_APP_CLIENT_SECRET', '').strip()
    callback_url = os.getenv('OH_GITHUB_OAUTH_CALLBACK_URL', '').strip()
    success_url = os.getenv('OH_GITHUB_OAUTH_SUCCESS_URL', '').strip()
    if not client_id or not client_secret or not callback_url or not success_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='GitHub OAuth is not configured',
        )
    if any(
        parsed.scheme != 'https' or not parsed.netloc
        for parsed in (urlparse(callback_url), urlparse(success_url))
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='GitHub OAuth callback and success URLs must use HTTPS',
        )
    return client_id, client_secret, callback_url, success_url


def _normalize_db_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


router = APIRouter(prefix='/oauth', tags=['OAuth'])
jwt_service_dependency = depends_jwt_service()
db_session_dependency = depends_db_session()


@router.get('/git-providers', response_model=list[GitProviderStatus])
async def list_git_provider_status(
    secrets_store: SecretsStore = Depends(get_secrets_store),
) -> list[GitProviderStatus]:
    user_secrets = await secrets_store.load()
    github_token = (
        user_secrets.provider_tokens.get(ProviderType.GITHUB) if user_secrets else None
    )
    connected = bool(github_token and github_token.token)
    return [
        GitProviderStatus(
            id=_GITHUB_PROVIDER_ID,
            provider_type=ProviderType.GITHUB.value,
            connected=connected,
            needs_reconnect=False,
        )
    ]


@router.post('/{provider_id}/link', response_model=GitProviderLinkResponse)
async def link_git_provider(
    provider_id: int,
    user_id: str | None = Depends(get_user_id),
    jwt_service: JwtService = jwt_service_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> GitProviderLinkResponse:
    if provider_id != _GITHUB_PROVIDER_ID:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Git provider not found',
        )
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authenticated user required',
        )
    client_id, _, callback_url, _ = _github_oauth_config()
    nonce = secrets.token_urlsafe(32)
    expires_at = utc_now() + _STATE_LIFETIME
    db_session.add(
        StoredGitHubOAuthState(
            nonce=nonce,
            user_id=user_id,
            expires_at=expires_at,
        )
    )
    await db_session.flush()
    state_token = jwt_service.create_jws_token(
        {
            'purpose': 'github-oauth',
            'nonce': nonce,
            'user_id': user_id,
        },
        expires_in=_STATE_LIFETIME,
    )
    start_url = 'https://github.com/login/oauth/authorize?' + urlencode(
        {
            'client_id': client_id,
            'redirect_uri': callback_url,
            'state': state_token,
        }
    )
    return GitProviderLinkResponse(start_url=start_url)


@router.get('/github/callback', response_model=None)
async def github_oauth_callback(
    code: str = Query(min_length=1),
    state_token: str = Query(alias='state', min_length=1),
    user_id: str | None = Depends(get_user_id),
    secrets_store: SecretsStore = Depends(get_secrets_store),
    jwt_service: JwtService = jwt_service_dependency,
    db_session: AsyncSession = db_session_dependency,
) -> RedirectResponse:
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authenticated user required',
        )
    client_id, client_secret, callback_url, success_url = _github_oauth_config()
    try:
        state_claims = jwt_service.verify_jws_token(state_token)
    except (ValueError, jwt.InvalidTokenError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid or expired OAuth state',
        ) from exc
    if (
        state_claims.get('purpose') != 'github-oauth'
        or state_claims.get('user_id') != user_id
        or not isinstance(state_claims.get('nonce'), str)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid OAuth state',
        )

    nonce = state_claims['nonce']
    stored_state = await db_session.get(StoredGitHubOAuthState, nonce)
    now = utc_now()
    if (
        stored_state is None
        or stored_state.user_id != user_id
        or stored_state.consumed_at is not None
        or _normalize_db_datetime(stored_state.expires_at) <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid or already used OAuth state',
        )
    consume_result = cast(
        CursorResult,
        await db_session.execute(
            update(StoredGitHubOAuthState)
            .where(
                StoredGitHubOAuthState.nonce == nonce,
                StoredGitHubOAuthState.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        ),
    )
    if consume_result.rowcount != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='OAuth state was already used',
        )
    await db_session.commit()

    async with httpx.AsyncClient(verify=httpx_verify_option()) as client:
        response = await client.post(
            'https://github.com/login/oauth/access_token',
            headers={'Accept': 'application/json'},
            data={
                'client_id': client_id,
                'client_secret': client_secret,
                'code': code,
                'redirect_uri': callback_url,
            },
        )
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='GitHub token exchange failed',
        )
    token_response = response.json()
    access_token = token_response.get('access_token')
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='GitHub authorization failed',
        )

    github = GitHubService(token=SecretStr(access_token))
    github_user = await github.get_user()
    existing = await secrets_store.load()
    provider_tokens = dict(existing.provider_tokens) if existing else {}
    provider_tokens[ProviderType.GITHUB] = ProviderToken(
        token=SecretStr(access_token),
        user_id=github_user.id,
        host='github.com',
    )
    await secrets_store.store(
        Secrets(
            provider_tokens=MappingProxyType(provider_tokens),
            custom_secrets=existing.custom_secrets if existing else {},
        )
    )

    return RedirectResponse(success_url, status_code=status.HTTP_303_SEE_OTHER)


@router.delete('/{provider_id}/link')
async def unlink_git_provider(
    provider_id: int,
    secrets_store: SecretsStore = Depends(get_secrets_store),
) -> dict[str, bool]:
    if provider_id != _GITHUB_PROVIDER_ID:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Git provider not found',
        )
    existing = await secrets_store.load()
    if existing and ProviderType.GITHUB in existing.provider_tokens:
        provider_tokens = dict(existing.provider_tokens)
        provider_tokens.pop(ProviderType.GITHUB)
        await secrets_store.store(
            Secrets(
                provider_tokens=MappingProxyType(provider_tokens),
                custom_secrets=existing.custom_secrets,
            )
        )
    return {'connected': False}
