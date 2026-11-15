from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Integer, String, Text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.settings.settings_models import Settings
from openhands.app_server.settings.settings_store import SettingsStore
from openhands.app_server.utils.sql_utils import Base


class StoredUserSettings(Base):
    __tablename__ = 'user_settings'

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
                settings = Settings()
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
