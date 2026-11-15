from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, String, Text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.settings.settings_models import Settings
from openhands.app_server.settings.settings_store import SettingsStore
from openhands.app_server.utils.sql_utils import Base
from openhands.sdk.llm import LLM
from openhands.sdk.mcp.config import MCPServer

_DEFAULT_LLM_PROFILE = 'default'
_FOUNDRY_WEB_SEARCH_MCP_NAME = 'foundry-web-search'
_FOUNDRY_WEB_SEARCH_MCP_AUTH_TYPE = 'azure_workload_identity'


def _default_hosted_settings() -> Settings:
    settings = Settings()
    agent_updates: dict[str, Any] = {}

    model = os.getenv('LLM_MODEL')
    if model:
        llm_updates = {'model': model}
        for env_name, field_name in (
            ('LLM_BASE_URL', 'base_url'),
            ('LLM_API_VERSION', 'api_version'),
            ('LLM_AUTH_TYPE', 'auth_type'),
        ):
            value = os.getenv(env_name)
            if value:
                llm_updates[field_name] = value

        llm = LLM.model_validate(llm_updates)
        agent_updates['llm'] = llm
        settings.llm_profiles.save(_DEFAULT_LLM_PROFILE, llm)
        settings.llm_profiles.active = _DEFAULT_LLM_PROFILE
        settings.title_llm_profile = _DEFAULT_LLM_PROFILE

    mcp_endpoint = os.getenv('FOUNDRY_WEB_SEARCH_MCP_ENDPOINT')
    mcp_auth_type = os.getenv('FOUNDRY_WEB_SEARCH_MCP_AUTH_TYPE')
    if bool(mcp_endpoint) != bool(mcp_auth_type):
        raise ValueError(
            'FOUNDRY_WEB_SEARCH_MCP_ENDPOINT and '
            'FOUNDRY_WEB_SEARCH_MCP_AUTH_TYPE must be configured together'
        )
    if mcp_endpoint:
        if mcp_auth_type != _FOUNDRY_WEB_SEARCH_MCP_AUTH_TYPE:
            raise ValueError(
                'FOUNDRY_WEB_SEARCH_MCP_AUTH_TYPE must be '
                f'{_FOUNDRY_WEB_SEARCH_MCP_AUTH_TYPE!r}'
            )
        agent_updates['mcp_config'] = {
            _FOUNDRY_WEB_SEARCH_MCP_NAME: MCPServer.model_validate(
                {
                    'url': mcp_endpoint,
                    'transport': 'streamable-http',
                    'auth': {'strategy': 'azure_workload_identity'},
                }
            )
        }

    if agent_updates:
        settings.agent_settings = settings.agent_settings.model_copy(
            update=agent_updates
        )
    return settings


class StoredUserSettings(Base):
    __tablename__ = 'oss_user_settings'

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


@dataclass
class SQLSettingsStore(SettingsStore):
    user_id: str
    _revision: int | None = None

    @staticmethod
    async def _dependencies():
        from openhands.app_server.config import get_global_config

        config = get_global_config()
        session_maker = await config.db_session.get_async_session_maker()
        jwt_injector = config.jwt
        assert jwt_injector is not None
        return session_maker, jwt_injector.get_jwt_service()

    async def load(
        self,
        *,
        resolve_agent_profile: bool = False,
        override_agent_profile_id: str | None = None,
    ) -> Settings:
        del resolve_agent_profile, override_agent_profile_id
        session_maker, jwt_service = await self._dependencies()
        async with session_maker() as session:
            row = await session.get(StoredUserSettings, self.user_id)
            if row is None:
                settings = _default_hosted_settings()
                encrypted_payload = jwt_service.encrypt_value(
                    settings.model_dump_json(
                        context={'expose_secrets': True, 'persist_settings': True}
                    )
                )
                session.add(
                    StoredUserSettings(
                        user_id=self.user_id,
                        encrypted_payload=encrypted_payload,
                        revision=0,
                    )
                )
                try:
                    await session.commit()
                    self._revision = 0
                    return settings
                except IntegrityError:
                    await session.rollback()
                    row = await session.get(StoredUserSettings, self.user_id)
                    if row is None:
                        raise

            self._revision = row.revision
            payload = jwt_service.decrypt_value(row.encrypted_payload)
            settings = Settings.model_validate_json(payload)
            settings.v1_enabled = True
            return settings

    async def store(self, settings: Settings) -> None:
        session_maker, jwt_service = await self._dependencies()
        encrypted_payload = jwt_service.encrypt_value(
            settings.model_dump_json(
                context={'expose_secrets': True, 'persist_settings': True}
            )
        )
        async with session_maker() as session:
            if self._revision is None:
                session.add(
                    StoredUserSettings(
                        user_id=self.user_id,
                        encrypted_payload=encrypted_payload,
                        revision=0,
                    )
                )
                try:
                    await session.commit()
                    self._revision = 0
                    return
                except IntegrityError as exc:
                    await session.rollback()
                    raise RuntimeError(
                        'Settings were updated by another request'
                    ) from exc

            revision = self._revision
            assert revision is not None
            result = await session.execute(
                update(StoredUserSettings)
                .where(
                    StoredUserSettings.user_id == self.user_id,
                    StoredUserSettings.revision == revision,
                )
                .values(
                    encrypted_payload=encrypted_payload,
                    revision=revision + 1,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise RuntimeError('Settings were updated by another request')
            await session.commit()
            self._revision = revision + 1

    @classmethod
    async def get_instance(cls, user_id: str | None) -> SQLSettingsStore:
        if not user_id:
            raise ValueError('A user id is required for SQL settings storage')
        return cls(user_id=user_id)

    async def get_org_marketplaces(self, user_id: str | None) -> list[dict]:
        return []
