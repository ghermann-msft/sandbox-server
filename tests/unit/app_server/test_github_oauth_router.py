from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pydantic import SecretStr

from openhands.agent_server.utils import utc_now
from openhands.app_server.app import app
from openhands.app_server.integrations.github.oauth_router import (
    StoredGitHubOAuthState,
    github_oauth_callback,
    link_git_provider,
    list_git_provider_status,
)
from openhands.app_server.integrations.service_types import ProviderType, UserGitInfo
from openhands.app_server.organization.organization_models import (
    OrganizationKind,
    StoredOrganization,
)
from openhands.app_server.secrets.secrets_models import Secrets
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.utils.encryption_key import EncryptionKey

_OAUTH_ENV = {
    'GITHUB_APP_CLIENT_ID': 'client-id',
    'GITHUB_APP_CLIENT_SECRET': 'client-secret',
    'OH_GITHUB_OAUTH_CALLBACK_URL': 'https://halo.example/oauth/github/callback',
    'OH_GITHUB_OAUTH_SUCCESS_URL': ('https://halo.example/settings/integrations'),
}
_PERSONAL_ORG_ID = UUID('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
_HALO_ORG_ID = UUID('c7d82db0-1e82-5c36-ad45-601c5cd3b646')


def test_canvas_oauth_routes_are_registered_at_root():
    route_paths = app.openapi()['paths']

    assert '/oauth/git-providers' in route_paths
    assert '/oauth/{provider_id}/link' in route_paths
    assert '/oauth/github/callback' in route_paths
    assert '/api/organizations/{organization_id}/connections' in route_paths
    assert '/api/organizations/{organization_id}/connections/{kind}' in route_paths
    assert '/api/organizations/{organization_id}/profiles' in route_paths
    assert '/api/organizations/{organization_id}/profiles/{name}' in route_paths
    assert (
        '/api/organizations/{organization_id}/profiles/{name}/activate' in route_paths
    )
    assert '/api/organizations/{organization_id}/profiles/{name}/rename' in route_paths


def _jwt_service() -> JwtService:
    return JwtService([EncryptionKey(id='test', key=SecretStr('oauth-state-key'))])


@pytest.mark.asyncio
async def test_status_uses_canvas_compatibility_shape():
    store = MagicMock()
    store.load = AsyncMock(return_value=Secrets())
    db_session = MagicMock()
    db_session.get = AsyncMock(
        return_value=StoredOrganization(
            id=_PERSONAL_ORG_ID,
            name='Personal',
            kind=OrganizationKind.PERSONAL.value,
        )
    )
    organization_service = MagicMock()
    organization_service.resolve_organization_id = AsyncMock(
        return_value=_PERSONAL_ORG_ID
    )

    with patch(
        'openhands.app_server.integrations.github.oauth_router.OrganizationService',
        return_value=organization_service,
    ):
        result = await list_git_provider_status(
            requested_org_id=_PERSONAL_ORG_ID,
            secrets_store=store,
            user_context=MagicMock(),
            jwt_service=_jwt_service(),
            db_session=db_session,
        )

    assert [item.model_dump() for item in result] == [
        {
            'id': 1,
            'provider_type': 'github',
            'connected': False,
            'needs_reconnect': False,
        }
    ]


@pytest.mark.asyncio
async def test_link_returns_absolute_github_start_url():
    db_session = MagicMock()
    db_session.add = MagicMock()
    db_session.flush = AsyncMock()
    organization_service = MagicMock()
    organization_service.resolve_organization_id = AsyncMock(return_value=_HALO_ORG_ID)
    organization_service.require_any_permission = AsyncMock()
    with (
        patch.dict('os.environ', _OAUTH_ENV, clear=False),
        patch(
            'openhands.app_server.integrations.github.oauth_router.OrganizationService',
            return_value=organization_service,
        ),
    ):
        result = await link_git_provider(
            provider_id=1,
            requested_org_id=_HALO_ORG_ID,
            user_id='entra:t:u',
            user_context=MagicMock(),
            jwt_service=_jwt_service(),
            db_session=db_session,
        )

    parsed = urlparse(result.start_url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == 'https'
    assert parsed.netloc == 'github.com'
    assert params['client_id'] == ['client-id']
    assert params['redirect_uri'] == [_OAUTH_ENV['OH_GITHUB_OAUTH_CALLBACK_URL']]
    assert params['state'][0]
    stored_state = db_session.add.call_args.args[0]
    assert stored_state.organization_id == _HALO_ORG_ID
    claims = _jwt_service().verify_jws_token(params['state'][0])
    assert claims['organization_id'] == str(_HALO_ORG_ID)


@pytest.mark.asyncio
async def test_callback_rejects_replayed_state_before_token_exchange():
    jwt_service = _jwt_service()
    state_token = jwt_service.create_jws_token(
        {
            'purpose': 'github-oauth',
            'nonce': 'nonce',
            'user_id': 'entra:t:u',
            'organization_id': str(_HALO_ORG_ID),
        },
        expires_in=timedelta(minutes=10),
    )
    db_session = MagicMock()
    db_session.get = AsyncMock(
        return_value=StoredGitHubOAuthState(
            nonce='nonce',
            user_id='entra:t:u',
            organization_id=_HALO_ORG_ID,
            expires_at=utc_now() + timedelta(minutes=5),
            consumed_at=utc_now(),
        )
    )
    store = MagicMock()
    with patch.dict('os.environ', _OAUTH_ENV, clear=False):
        with pytest.raises(HTTPException, match='already used'):
            await github_oauth_callback(
                code='code',
                state_token=state_token,
                user_id='entra:t:u',
                secrets_store=store,
                user_context=MagicMock(),
                jwt_service=jwt_service,
                db_session=db_session,
            )

    store.store.assert_not_called()


@pytest.mark.asyncio
async def test_callback_rejects_state_issued_for_another_user():
    jwt_service = _jwt_service()
    state_token = jwt_service.create_jws_token(
        {
            'purpose': 'github-oauth',
            'nonce': 'nonce',
            'user_id': 'entra:t:alice',
            'organization_id': str(_HALO_ORG_ID),
        },
        expires_in=timedelta(minutes=10),
    )
    db_session = MagicMock()
    store = MagicMock()

    with patch.dict('os.environ', _OAUTH_ENV, clear=False):
        with pytest.raises(HTTPException, match='Invalid OAuth state'):
            await github_oauth_callback(
                code='code',
                state_token=state_token,
                user_id='entra:t:bob',
                secrets_store=store,
                user_context=MagicMock(),
                jwt_service=jwt_service,
                db_session=db_session,
            )

    db_session.get.assert_not_called()
    store.store.assert_not_called()


@pytest.mark.asyncio
async def test_callback_consumes_state_and_persists_github_token():
    jwt_service = _jwt_service()
    state_token = jwt_service.create_jws_token(
        {
            'purpose': 'github-oauth',
            'nonce': 'nonce',
            'user_id': 'entra:t:u',
            'organization_id': str(_PERSONAL_ORG_ID),
        },
        expires_in=timedelta(minutes=10),
    )
    db_session = MagicMock()
    state = StoredGitHubOAuthState(
        nonce='nonce',
        user_id='entra:t:u',
        organization_id=_PERSONAL_ORG_ID,
        expires_at=utc_now() + timedelta(minutes=5),
        consumed_at=None,
    )
    personal = StoredOrganization(
        id=_PERSONAL_ORG_ID,
        name='Personal',
        kind=OrganizationKind.PERSONAL.value,
    )
    db_session.get = AsyncMock(side_effect=[state, personal])
    update_result = MagicMock()
    update_result.rowcount = 1
    db_session.execute = AsyncMock(return_value=update_result)
    db_session.commit = AsyncMock()

    response = MagicMock()
    response.is_error = False
    response.json.return_value = {'access_token': 'github-token'}
    client = AsyncMock()
    client.post.return_value = response
    client_context = AsyncMock()
    client_context.__aenter__.return_value = client

    github = MagicMock()
    github.get_user = AsyncMock(
        return_value=UserGitInfo(
            id='123',
            login='octocat',
            avatar_url='https://avatars.example/octocat',
        )
    )
    store = MagicMock()
    store.load = AsyncMock(return_value=None)
    store.store = AsyncMock()
    organization_service = MagicMock()
    organization_service.require_any_permission = AsyncMock()

    with (
        patch.dict('os.environ', _OAUTH_ENV, clear=False),
        patch(
            'openhands.app_server.integrations.github.oauth_router.httpx.AsyncClient',
            return_value=client_context,
        ),
        patch(
            'openhands.app_server.integrations.github.oauth_router.GitHubService',
            return_value=github,
        ),
        patch(
            'openhands.app_server.integrations.github.oauth_router.OrganizationService',
            return_value=organization_service,
        ),
    ):
        result = await github_oauth_callback(
            code='code',
            state_token=state_token,
            user_id='entra:t:u',
            secrets_store=store,
            user_context=MagicMock(),
            jwt_service=jwt_service,
            db_session=db_session,
        )

    assert isinstance(result, RedirectResponse)
    assert result.status_code == 303
    assert result.headers['location'] == _OAUTH_ENV['OH_GITHUB_OAUTH_SUCCESS_URL']
    db_session.commit.assert_awaited_once()
    saved = store.store.await_args.args[0]
    assert (
        saved.provider_tokens[ProviderType.GITHUB].token.get_secret_value()
        == 'github-token'
    )


@pytest.mark.asyncio
async def test_callback_stores_shared_github_connection_for_state_organization():
    jwt_service = _jwt_service()
    state_token = jwt_service.create_jws_token(
        {
            'purpose': 'github-oauth',
            'nonce': 'nonce',
            'user_id': 'entra:t:u',
            'organization_id': str(_HALO_ORG_ID),
        },
        expires_in=timedelta(minutes=10),
    )
    state = StoredGitHubOAuthState(
        nonce='nonce',
        user_id='entra:t:u',
        organization_id=_HALO_ORG_ID,
        expires_at=utc_now() + timedelta(minutes=5),
        consumed_at=None,
    )
    shared = StoredOrganization(
        id=_HALO_ORG_ID,
        name='Halo Studios',
        kind=OrganizationKind.SHARED.value,
    )
    db_session = MagicMock()
    db_session.get = AsyncMock(side_effect=[state, shared])
    update_result = MagicMock(rowcount=1)
    db_session.execute = AsyncMock(return_value=update_result)
    db_session.commit = AsyncMock()

    response = MagicMock()
    response.is_error = False
    response.json.return_value = {'access_token': 'github-token'}
    client = AsyncMock()
    client.post.return_value = response
    client_context = AsyncMock()
    client_context.__aenter__.return_value = client
    github = MagicMock()
    github.get_user = AsyncMock(
        return_value=UserGitInfo(
            id='123',
            login='octocat',
            avatar_url='https://avatars.example/octocat',
        )
    )
    personal_store = MagicMock()
    personal_store.store = AsyncMock()
    organization_service = MagicMock()
    organization_service.require_any_permission = AsyncMock()
    connection_service = MagicMock()
    connection_service.store_github_token = AsyncMock()

    with (
        patch.dict('os.environ', _OAUTH_ENV, clear=False),
        patch(
            'openhands.app_server.integrations.github.oauth_router.httpx.AsyncClient',
            return_value=client_context,
        ),
        patch(
            'openhands.app_server.integrations.github.oauth_router.GitHubService',
            return_value=github,
        ),
        patch(
            'openhands.app_server.integrations.github.oauth_router.OrganizationService',
            return_value=organization_service,
        ),
        patch(
            'openhands.app_server.integrations.github.oauth_router.OrganizationConnectionService',
            return_value=connection_service,
        ),
    ):
        await github_oauth_callback(
            code='code',
            state_token=state_token,
            user_id='entra:t:u',
            secrets_store=personal_store,
            user_context=MagicMock(),
            jwt_service=jwt_service,
            db_session=db_session,
        )

    args = connection_service.store_github_token.await_args.args
    assert args[0] == _HALO_ORG_ID
    assert args[1].token.get_secret_value() == 'github-token'
    personal_store.store.assert_not_awaited()
