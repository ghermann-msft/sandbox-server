import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from openhands.agent_server.env_parser import from_env
from openhands.app_server.config import AppServerConfig
from openhands.app_server.errors import AuthError
from openhands.app_server.settings.server_managed_llm import (
    AZURE_OPENAI_SCOPE,
    ServerManagedLLM,
    load_server_managed_llm,
)
from openhands.app_server.settings.settings_models import Settings
from openhands.app_server.user.auth_user_context import (
    AuthUserContext,
    AuthUserContextInjector,
)
from openhands.app_server.user.trusted_identity import TrustedIdentity
from openhands.sdk.llm import LLM
from openhands.sdk.tool import SUB_AGENT_TOOL_NAME, Tool


def _managed_llm() -> ServerManagedLLM:
    return ServerManagedLLM(
        model='azure/gpt-5.6-sol',
        base_url='https://halo.openai.azure.com',
        api_version='2025-04-01-preview',
        auth_type='azure_workload_identity',
    )


def _clean_env() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in [
            'PATH',
            'HOME',
            'PYTHONPATH',
            'VIRTUAL_ENV',
            'TMPDIR',
            'TMP',
            'TEMP',
            'USERPROFILE',
            'HOMEDRIVE',
            'HOMEPATH',
        ]
        if key in os.environ
    }


def test_managed_mode_parses_app_config_and_builds_injector():
    env = _clean_env()
    env.update(
        {
            'OH_ALLOW_USER_LLM_CONFIGURATION': 'false',
            'LLM_MODEL': 'azure/gpt-5.6-sol',
            'LLM_BASE_URL': 'https://halo.openai.azure.com',
            'LLM_API_VERSION': '2025-04-01-preview',
            'LLM_AUTH_TYPE': 'azure_workload_identity',
        }
    )

    with patch.dict(os.environ, env, clear=True):
        config = from_env(AppServerConfig, 'OH')
        managed_llm = load_server_managed_llm(config.allow_user_llm_configuration)
        injector = AuthUserContextInjector.with_managed_llm(managed_llm)

    assert config.allow_user_llm_configuration is False
    assert managed_llm is not None
    assert managed_llm.model == 'azure/gpt-5.6-sol'
    assert managed_llm.base_url == 'https://halo.openai.azure.com'
    assert managed_llm.api_version == '2025-04-01-preview'
    assert managed_llm.auth_type == 'azure_workload_identity'
    assert injector.managed_llm is managed_llm
    assert 'managed_llm' not in AuthUserContextInjector.model_fields


def test_user_managed_mode_does_not_load_deployment_llm():
    with patch.dict(os.environ, {}, clear=True):
        assert load_server_managed_llm(True) is None


def test_managed_mode_requires_deployment_model():
    with (
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(ValidationError),
    ):
        load_server_managed_llm(False)


def test_auth_user_context_injector_accepts_managed_llm():
    managed_llm = _managed_llm()

    injector = AuthUserContextInjector.with_managed_llm(managed_llm)

    assert injector.managed_llm is managed_llm


@pytest.mark.asyncio
async def test_auth_user_context_injector_uses_verified_trusted_identity():
    object_id = '708bbdc7-c90e-4ac8-a3a0-e15e6c4db5c4'
    identity_service = Mock()
    identity_service.verify_request.return_value = TrustedIdentity(
        tenant_id='11111111-1111-1111-1111-111111111111',
        object_id=object_id,
    )

    @asynccontextmanager
    async def trusted_identity_context(state, request):
        yield identity_service

    user_auth = Mock()
    user_auth.get_user_id = AsyncMock(return_value=None)
    request = Request({'type': 'http', 'headers': [], 'state': {}})
    injector = AuthUserContextInjector()

    with (
        patch(
            'openhands.app_server.config.get_global_config',
            return_value=SimpleNamespace(trusted_identity=object()),
        ),
        patch(
            'openhands.app_server.config.get_trusted_identity_service',
            side_effect=trusted_identity_context,
        ),
        patch(
            'openhands.app_server.user.auth_user_context.get_user_auth',
            AsyncMock(return_value=user_auth),
        ),
    ):
        async with injector.context(request.state, request) as user_context:
            assert await user_context.get_user_id() == object_id

    identity_service.verify_request.assert_called_once_with(request)
    user_auth.get_user_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_user_context_injector_preserves_standard_auth_without_assertions():
    user_auth = Mock()
    user_auth.get_user_id = AsyncMock(return_value='standard-user')
    request = Request({'type': 'http', 'headers': [], 'state': {}})
    injector = AuthUserContextInjector()

    with (
        patch(
            'openhands.app_server.config.get_global_config',
            return_value=SimpleNamespace(trusted_identity=None),
        ),
        patch(
            'openhands.app_server.user.auth_user_context.get_user_auth',
            AsyncMock(return_value=user_auth),
        ),
    ):
        async with injector.context(request.state, request) as user_context:
            assert await user_context.get_user_id() == 'standard-user'

    user_auth.get_user_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_user_context_injector_rejects_invalid_trusted_identity():
    identity_service = Mock()
    identity_service.verify_request.side_effect = AuthError(
        'Invalid trusted identity assertion'
    )

    @asynccontextmanager
    async def trusted_identity_context(state, request):
        yield identity_service

    request = Request({'type': 'http', 'headers': [], 'state': {}})
    injector = AuthUserContextInjector()

    with (
        patch(
            'openhands.app_server.config.get_global_config',
            return_value=SimpleNamespace(trusted_identity=object()),
        ),
        patch(
            'openhands.app_server.config.get_trusted_identity_service',
            side_effect=trusted_identity_context,
        ),
        patch(
            'openhands.app_server.user.auth_user_context.get_user_auth',
            AsyncMock(),
        ) as get_user_auth,
        pytest.raises(AuthError, match='Invalid trusted identity assertion'),
    ):
        async with injector.context(request.state, request):
            pass

    get_user_auth.assert_not_awaited()


def test_azure_workload_identity_uses_cached_default_credential_provider():
    token_provider = Mock(return_value='token')
    llm = _managed_llm()

    with (
        patch(
            'openhands.app_server.settings.server_managed_llm.DefaultAzureCredential'
        ) as credential_class,
        patch(
            'openhands.app_server.settings.server_managed_llm.get_bearer_token_provider',
            return_value=token_provider,
        ) as provider_factory,
    ):
        first = llm._azure_workload_identity_kwargs()
        second = llm._azure_workload_identity_kwargs()

    assert first == {'azure_ad_token_provider': token_provider}
    assert second == first
    credential_class.assert_called_once_with()
    provider_factory.assert_called_once_with(
        credential_class.return_value, AZURE_OPENAI_SCOPE
    )


def test_azure_workload_identity_replaces_api_key_transport_auth():
    token_provider = Mock(return_value='token')
    llm = _managed_llm()

    with (
        patch.object(
            LLM,
            '_prepare_transport_kwargs',
            return_value={
                'model': 'azure/gpt-5.6-sol',
                'api_key': None,
                'messages': [],
            },
        ),
        patch.object(
            llm,
            '_azure_workload_identity_kwargs',
            return_value={'azure_ad_token_provider': token_provider},
        ),
    ):
        result = llm._prepare_transport_kwargs(
            messages=[],
            enable_streaming=False,
        )

    assert 'api_key' not in result
    assert result['azure_ad_token_provider'] is token_provider


@pytest.mark.asyncio
async def test_absent_settings_use_server_managed_llm():
    user_auth = AsyncMock()
    user_auth.get_user_id = AsyncMock(return_value='entra-object-id')
    user_auth.get_user_settings = AsyncMock(return_value=None)
    managed_llm = _managed_llm()
    ctx = AuthUserContext(user_auth=user_auth, managed_llm=managed_llm)

    user_info = await ctx.get_user_info(resolve_agent_profile=True)

    assert user_info.agent_settings.llm is managed_llm
    assert user_info.agent_settings.llm.model == 'azure/gpt-5.6-sol'
    assert user_info.agent_settings.llm.auth_type == 'azure_workload_identity'
    assert any(
        tool.name == SUB_AGENT_TOOL_NAME
        for tool in user_info.agent_settings.tools or []
    )


@pytest.mark.asyncio
async def test_managed_llm_is_authoritative_over_persisted_settings():
    user_auth = AsyncMock()
    user_auth.get_user_id = AsyncMock(return_value='entra-object-id')
    user_auth.get_user_settings = AsyncMock(
        return_value=Settings(
            agent_settings={'llm': {'model': 'user/model', 'api_key': 'secret'}}
        )
    )
    managed_llm = _managed_llm()
    ctx = AuthUserContext(user_auth=user_auth, managed_llm=managed_llm)

    user_info = await ctx.get_user_info(resolve_agent_profile=True)

    assert user_info.agent_settings.llm is managed_llm
    if 'enable_sub_agents' in user_info.agent_settings.model_fields:
        assert user_info.agent_settings.enable_sub_agents is True
    else:
        assert any(
            tool.name == SUB_AGENT_TOOL_NAME
            for tool in user_info.agent_settings.tools or []
        )


@pytest.mark.asyncio
async def test_managed_llm_does_not_overwrite_user_managed_profile_tools():
    user_auth = AsyncMock()
    user_auth.get_user_id = AsyncMock(return_value='entra-object-id')
    user_auth.get_user_settings = AsyncMock(
        return_value=Settings(
            active_agent_profile_id='00000000-0000-0000-0000-000000000001',
            agent_settings={
                'llm': {'model': 'user/model', 'api_key': 'secret'},
                'tools': [Tool(name='terminal')],
            },
        )
    )
    managed_llm = _managed_llm()
    ctx = AuthUserContext(user_auth=user_auth, managed_llm=managed_llm)

    user_info = await ctx.get_user_info(resolve_agent_profile=True)

    assert user_info.agent_settings.llm is managed_llm
    assert [tool.name for tool in user_info.agent_settings.tools or []] == ['terminal']


@pytest.mark.asyncio
async def test_user_llm_is_preserved_when_managed_mode_is_disabled():
    user_auth = AsyncMock()
    user_auth.get_user_id = AsyncMock(return_value='entra-object-id')
    user_auth.get_user_settings = AsyncMock(
        return_value=Settings(
            agent_settings={'llm': {'model': 'user/model', 'api_key': 'secret'}}
        )
    )
    ctx = AuthUserContext(user_auth=user_auth)

    user_info = await ctx.get_user_info(resolve_agent_profile=True)

    assert user_info.agent_settings.llm.model == 'user/model'
    assert user_info.agent_settings.llm.api_key is not None
    assert user_info.agent_settings.llm.api_key.get_secret_value() == 'secret'
