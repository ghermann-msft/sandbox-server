from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Integer, String, Text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.secrets.secrets_models import Secrets
from openhands.app_server.secrets.secrets_store import SecretsStore
from openhands.app_server.utils.sql_utils import Base


class StoredUserSecrets(Base):
    __tablename__ = 'user_secrets'

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


@dataclass
class SQLSecretsStore(SecretsStore):
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

    async def load(self) -> Secrets | None:
        session_maker, jwt_service = await self._dependencies()
        async with session_maker() as session:
            row = await session.get(StoredUserSecrets, self.user_id)
            if row is None:
                self._revision = None
                return None
            self._revision = row.revision
            payload = jwt_service.decrypt_value(row.encrypted_payload)
            return Secrets.model_validate_json(payload)

    async def store(self, secrets: Secrets) -> None:
        session_maker, jwt_service = await self._dependencies()
        encrypted_payload = jwt_service.encrypt_value(
            secrets.model_dump_json(context={'expose_secrets': True})
        )
        async with session_maker() as session:
            if self._revision is None:
                session.add(
                    StoredUserSecrets(
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
                        'Secrets were updated by another request'
                    ) from exc

            revision = self._revision
            assert revision is not None
            result = await session.execute(
                update(StoredUserSecrets)
                .where(
                    StoredUserSecrets.user_id == self.user_id,
                    StoredUserSecrets.revision == revision,
                )
                .values(
                    encrypted_payload=encrypted_payload,
                    revision=revision + 1,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise RuntimeError('Secrets were updated by another request')
            await session.commit()
            self._revision = revision + 1

    @classmethod
    async def get_instance(cls, user_id: str | None) -> SQLSecretsStore:
        if not user_id:
            raise ValueError('A user id is required for SQL secrets storage')
        return cls(user_id=user_id)
