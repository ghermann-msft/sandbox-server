from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openhands.app_server.integrations.github.oauth_router import StoredGitHubOAuthState
from openhands.app_server.integrations.provider import ProviderToken
from openhands.app_server.integrations.service_types import ProviderType
from openhands.app_server.secrets.secrets_models import Secrets
from openhands.app_server.secrets.sql_secrets_store import (
    SQLSecretsStore,
    StoredUserSecrets,
)
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.settings.sql_settings_store import (
    SQLSettingsStore,
    StoredUserSettings,
)
from openhands.app_server.utils.encryption_key import EncryptionKey


@pytest.fixture
async def sql_dependencies(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "users.db"}')
    async with engine.begin() as connection:
        await connection.run_sync(StoredUserSettings.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    jwt_service = JwtService(
        [EncryptionKey(id='test', key=SecretStr('storage-encryption-key'))]
    )
    yield session_maker, jwt_service
    await engine.dispose()


@pytest.mark.asyncio
async def test_settings_bootstrap_and_cross_user_isolation(sql_dependencies):
    dependencies = AsyncMock(return_value=sql_dependencies)
    with patch.object(SQLSettingsStore, '_dependencies', dependencies):
        alice = SQLSettingsStore('entra:t:alice')
        bob = SQLSettingsStore('entra:t:bob')

        alice_settings = await alice.load()
        alice_settings.language = 'fr'
        await alice.store(alice_settings)

        bob_settings = await bob.load()
        alice_reloaded = await SQLSettingsStore('entra:t:alice').load()

    assert bob_settings.language is None
    assert alice_reloaded.language == 'fr'


def test_sql_user_stores_use_collision_proof_oss_tables():
    assert StoredUserSettings.__tablename__ == 'oss_user_settings'
    assert StoredUserSecrets.__tablename__ == 'oss_user_secrets'
    assert StoredGitHubOAuthState.__tablename__ == 'oss_github_oauth_state'


@pytest.mark.asyncio
async def test_settings_bootstrap_uses_deployment_llm_environment(
    sql_dependencies, monkeypatch
):
    monkeypatch.setenv('LLM_MODEL', 'azure/gpt-5.1')
    monkeypatch.setenv(
        'LLM_BASE_URL',
        'https://example.openai.azure.com/openai/deployments/gpt-5.1',
    )
    monkeypatch.setenv('LLM_API_VERSION', '2025-04-01-preview')
    monkeypatch.setenv('LLM_AUTH_TYPE', 'api_key')
    dependencies = AsyncMock(return_value=sql_dependencies)

    with patch.object(SQLSettingsStore, '_dependencies', dependencies):
        settings = await SQLSettingsStore('entra:t:user').load()

    llm = settings.agent_settings.llm
    assert llm.model == 'azure/gpt-5.1'
    assert llm.base_url == 'https://example.openai.azure.com/openai/deployments/gpt-5.1'
    assert llm.api_version == '2025-04-01-preview'
    assert llm.auth_type == 'api_key'
    assert settings.llm_profiles.active == 'default'
    assert settings.llm_profiles.get('default') == llm
    assert settings.title_llm_profile == 'default'


@pytest.mark.asyncio
async def test_settings_bootstrap_without_llm_environment_keeps_defaults(
    sql_dependencies, monkeypatch
):
    for name in ('LLM_MODEL', 'LLM_BASE_URL', 'LLM_API_VERSION', 'LLM_AUTH_TYPE'):
        monkeypatch.delenv(name, raising=False)
    dependencies = AsyncMock(return_value=sql_dependencies)

    with patch.object(SQLSettingsStore, '_dependencies', dependencies):
        settings = await SQLSettingsStore('entra:t:user').load()

    assert settings.llm_profiles.active is None
    assert settings.llm_profiles.profiles == {}
    assert settings.title_llm_profile is None


@pytest.mark.asyncio
async def test_settings_bootstrap_does_not_replace_existing_settings(
    sql_dependencies, monkeypatch
):
    dependencies = AsyncMock(return_value=sql_dependencies)
    with patch.object(SQLSettingsStore, '_dependencies', dependencies):
        store = SQLSettingsStore('entra:t:user')
        settings = await store.load()
        settings.language = 'fr'
        await store.store(settings)

        monkeypatch.setenv('LLM_MODEL', 'azure/gpt-5.1')
        reloaded = await SQLSettingsStore('entra:t:user').load()

    assert reloaded.language == 'fr'
    assert reloaded.llm_profiles.profiles == {}
    assert reloaded.title_llm_profile is None


@pytest.mark.asyncio
async def test_settings_and_secrets_are_encrypted_at_rest(sql_dependencies):
    session_maker, _ = sql_dependencies
    dependencies = AsyncMock(return_value=sql_dependencies)
    with (
        patch.object(SQLSettingsStore, '_dependencies', dependencies),
        patch.object(SQLSecretsStore, '_dependencies', dependencies),
    ):
        settings_store = SQLSettingsStore('entra:t:user')
        settings = await settings_store.load()
        settings.search_api_key = SecretStr('settings-secret-value')
        await settings_store.store(settings)

        secrets_store = SQLSecretsStore('entra:t:user')
        await secrets_store.store(
            Secrets(
                provider_tokens={
                    ProviderType.GITHUB: ProviderToken(
                        token=SecretStr('github-secret-value')
                    )
                }
            )
        )

        async with session_maker() as session:
            settings_row = await session.get(StoredUserSettings, 'entra:t:user')
            secrets_row = await session.get(StoredUserSecrets, 'entra:t:user')
            table_names = set(
                (
                    await session.execute(
                        text("SELECT name FROM sqlite_master WHERE type = 'table'")
                    )
                )
                .scalars()
                .all()
            )
        reloaded_secrets = await SQLSecretsStore('entra:t:user').load()

    assert 'oss_user_settings' in table_names
    assert 'oss_user_secrets' in table_names
    assert 'user_settings' not in table_names
    assert 'user_secrets' not in table_names
    assert 'settings-secret-value' not in settings_row.encrypted_payload
    assert 'github-secret-value' not in secrets_row.encrypted_payload
    assert (
        reloaded_secrets.provider_tokens[ProviderType.GITHUB].token.get_secret_value()
        == 'github-secret-value'
    )


@pytest.mark.asyncio
async def test_stale_settings_write_is_rejected(sql_dependencies):
    dependencies = AsyncMock(return_value=sql_dependencies)
    with patch.object(SQLSettingsStore, '_dependencies', dependencies):
        first = SQLSettingsStore('entra:t:user')
        second = SQLSettingsStore('entra:t:user')
        first_settings = await first.load()
        second_settings = await second.load()

        first_settings.language = 'fr'
        await first.store(first_settings)
        second_settings.language = 'de'
        with pytest.raises(RuntimeError, match='another request'):
            await second.store(second_settings)


@pytest.mark.asyncio
async def test_concurrent_first_secret_write_is_rejected(sql_dependencies):
    dependencies = AsyncMock(return_value=sql_dependencies)
    with patch.object(SQLSecretsStore, '_dependencies', dependencies):
        first = SQLSecretsStore('entra:t:user')
        second = SQLSecretsStore('entra:t:user')
        assert await first.load() is None
        assert await second.load() is None

        await first.store(Secrets(custom_secrets={'first': 'value'}))
        with pytest.raises(RuntimeError, match='another request'):
            await second.store(Secrets(custom_secrets={'second': 'value'}))
