from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.errors import SandboxError
from openhands.app_server.utils.sql_utils import Base, UtcDateTime

ENTRA_USER_ID_PREFIX = 'entra'


def parse_entra_user_id(user_id: str | None) -> tuple[str, str] | None:
    if not user_id:
        return None
    prefix, separator, remainder = user_id.partition(':')
    tenant_id, nested_separator, object_id = remainder.partition(':')
    if (
        prefix != ENTRA_USER_ID_PREFIX
        or not separator
        or not nested_separator
        or not tenant_id
        or not object_id
        or ':' in object_id
    ):
        return None
    return tenant_id, object_id


def format_entra_user_id(tenant_id: str, object_id: str) -> str:
    return f'{ENTRA_USER_ID_PREFIX}:{tenant_id}:{object_id}'


def can_access_sandbox_credentials(
    user_id: str | None, owner_user_id: str | None
) -> bool:
    if owner_user_id is not None:
        return owner_user_id == user_id
    return parse_entra_user_id(user_id) is None


class StoredSandboxAccess(Base):
    __tablename__ = 'v1_sandbox_access'

    sandbox_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    owner_object_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    access_type: Mapped[str] = mapped_column(String, nullable=False, default='PERSONAL')
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), index=True
    )


@dataclass
class SandboxAccessService:
    db_session: AsyncSession
    user_id: str | None = None

    def require_owner(self) -> tuple[str, str]:
        identity = parse_entra_user_id(self.user_id)
        if identity is None:
            raise SandboxError('A trusted Entra sandbox identity is required')
        return identity

    async def create_personal_sandbox(self, sandbox_id: str) -> str:
        tenant_id, object_id = self.require_owner()
        self.db_session.add(
            StoredSandboxAccess(
                sandbox_id=sandbox_id,
                tenant_id=tenant_id,
                owner_object_id=object_id,
            )
        )
        await self.db_session.flush()
        return format_entra_user_id(tenant_id, object_id)

    async def owner_user_id(self, sandbox_id: str) -> str | None:
        owners = await self.owner_user_ids([sandbox_id])
        return owners.get(sandbox_id)

    async def owner_user_ids(self, sandbox_ids: Iterable[str]) -> dict[str, str]:
        unique_ids = set(sandbox_ids)
        if not unique_ids:
            return {}
        result = await self.db_session.execute(
            select(
                StoredSandboxAccess.sandbox_id,
                StoredSandboxAccess.tenant_id,
                StoredSandboxAccess.owner_object_id,
            ).where(StoredSandboxAccess.sandbox_id.in_(unique_ids))
        )
        return {
            owner.sandbox_id: format_entra_user_id(
                owner.tenant_id, owner.owner_object_id
            )
            for owner in result.all()
        }

    async def delete_sandbox(self, sandbox_id: str) -> None:
        await self.db_session.execute(
            delete(StoredSandboxAccess).where(
                StoredSandboxAccess.sandbox_id == sandbox_id
            )
        )
