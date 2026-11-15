from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
        reloaded_secrets = await SQLSecretsStore('entra:t:user').load()

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
