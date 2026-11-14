from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, String, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.errors import PermissionsError
from openhands.app_server.sandbox.sandbox_models import (
    SandboxAccessType,
    SandboxAcl,
    SandboxAclUpdate,
)
from openhands.app_server.user.trusted_identity import TrustedIdentity
from openhands.app_server.utils.sql_utils import Base, UtcDateTime


class StoredSandboxAccess(Base):
    __tablename__ = 'v1_sandbox_access'

    sandbox_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    owner_object_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    access_type: Mapped[str] = mapped_column(
        String, nullable=False, default=SandboxAccessType.PERSONAL.value
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), index=True
    )


class StoredSandboxMember(Base):
    __tablename__ = 'v1_sandbox_member'

    sandbox_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
    member_object_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ['sandbox_id'],
            ['v1_sandbox_access.sandbox_id'],
            ondelete='CASCADE',
        ),
    )


@dataclass
class SandboxAccessService:
    db_session: AsyncSession
    identity: TrustedIdentity | None = None

    def _require_identity(self) -> TrustedIdentity:
        if self.identity is None:
            raise PermissionsError('A trusted identity is required')
        return self.identity

    async def create_personal_sandbox(self, sandbox_id: str) -> None:
        identity = self._require_identity()
        self.db_session.add(
            StoredSandboxAccess(
                sandbox_id=sandbox_id,
                tenant_id=identity.tenant_id,
                owner_object_id=identity.object_id,
                access_type=SandboxAccessType.PERSONAL.value,
            )
        )
        await self.db_session.flush()

    async def accessible_sandbox_ids(self) -> set[str]:
        identity = self._require_identity()
        statement = (
            select(StoredSandboxAccess.sandbox_id)
            .outerjoin(
                StoredSandboxMember,
                (StoredSandboxMember.sandbox_id == StoredSandboxAccess.sandbox_id)
                & (StoredSandboxMember.tenant_id == StoredSandboxAccess.tenant_id),
            )
            .where(
                StoredSandboxAccess.tenant_id == identity.tenant_id,
                or_(
                    StoredSandboxAccess.owner_object_id == identity.object_id,
                    StoredSandboxMember.member_object_id == identity.object_id,
                ),
            )
        )
        result = await self.db_session.execute(statement)
        return set(result.scalars().all())

    async def can_access(self, sandbox_id: str) -> bool:
        return await self._get_access(sandbox_id, owner_only=False) is not None

    async def is_owner(self, sandbox_id: str) -> bool:
        return await self._get_access(sandbox_id, owner_only=True) is not None

    async def get_acl(self, sandbox_id: str) -> SandboxAcl | None:
        identity = self._require_identity()
        access = await self._get_access(sandbox_id, owner_only=False)
        if access is None:
            return None
        members = await self.db_session.execute(
            select(StoredSandboxMember.member_object_id)
            .where(
                StoredSandboxMember.sandbox_id == sandbox_id,
                StoredSandboxMember.tenant_id == identity.tenant_id,
            )
            .order_by(StoredSandboxMember.member_object_id)
        )
        return SandboxAcl(
            sandbox_id=sandbox_id,
            access_type=SandboxAccessType(access.access_type),
            owner_object_id=access.owner_object_id,
            member_object_ids=list(members.scalars().all()),
            is_owner=access.owner_object_id == identity.object_id,
        )

    async def update_acl(
        self, sandbox_id: str, update: SandboxAclUpdate
    ) -> SandboxAcl | None:
        identity = self._require_identity()
        access = await self._get_access(sandbox_id, owner_only=True)
        if access is None:
            if await self.can_access(sandbox_id):
                raise PermissionsError('Only the sandbox owner can update its ACL')
            return None

        member_ids = set(update.member_object_ids)
        if identity.object_id in member_ids:
            raise PermissionsError('The sandbox owner cannot also be an ACL member')
        if update.access_type == SandboxAccessType.PERSONAL and member_ids:
            raise PermissionsError('Personal sandboxes cannot have ACL members')

        access.access_type = update.access_type.value
        await self.db_session.execute(
            delete(StoredSandboxMember).where(
                StoredSandboxMember.sandbox_id == sandbox_id
            )
        )
        if update.access_type == SandboxAccessType.TEAM:
            self.db_session.add_all(
                [
                    StoredSandboxMember(
                        sandbox_id=sandbox_id,
                        tenant_id=identity.tenant_id,
                        member_object_id=member_id,
                    )
                    for member_id in sorted(member_ids)
                ]
            )
        await self.db_session.flush()
        return await self.get_acl(sandbox_id)

    async def delete_sandbox(self, sandbox_id: str) -> None:
        await self.db_session.execute(
            delete(StoredSandboxMember).where(
                StoredSandboxMember.sandbox_id == sandbox_id
            )
        )
        await self.db_session.execute(
            delete(StoredSandboxAccess).where(
                StoredSandboxAccess.sandbox_id == sandbox_id
            )
        )

    async def owner_object_id(self, sandbox_id: str) -> str | None:
        result = await self.db_session.execute(
            select(StoredSandboxAccess.owner_object_id).where(
                StoredSandboxAccess.sandbox_id == sandbox_id
            )
        )
        return result.scalar_one_or_none()

    async def owner_identity(self, sandbox_id: str) -> TrustedIdentity | None:
        result = await self.db_session.execute(
            select(
                StoredSandboxAccess.tenant_id,
                StoredSandboxAccess.owner_object_id,
            ).where(StoredSandboxAccess.sandbox_id == sandbox_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return TrustedIdentity(tenant_id=row.tenant_id, object_id=row.owner_object_id)

    async def access_type(self, sandbox_id: str) -> SandboxAccessType | None:
        result = await self.db_session.execute(
            select(StoredSandboxAccess.access_type).where(
                StoredSandboxAccess.sandbox_id == sandbox_id
            )
        )
        value = result.scalar_one_or_none()
        return SandboxAccessType(value) if value else None

    async def _get_access(
        self, sandbox_id: str, owner_only: bool
    ) -> StoredSandboxAccess | None:
        identity = self._require_identity()
        statement = select(StoredSandboxAccess).where(
            StoredSandboxAccess.sandbox_id == sandbox_id,
            StoredSandboxAccess.tenant_id == identity.tenant_id,
        )
        if owner_only:
            statement = statement.where(
                StoredSandboxAccess.owner_object_id == identity.object_id
            )
        else:
            statement = statement.outerjoin(
                StoredSandboxMember,
                (StoredSandboxMember.sandbox_id == StoredSandboxAccess.sandbox_id)
                & (StoredSandboxMember.tenant_id == StoredSandboxAccess.tenant_id),
            ).where(
                or_(
                    StoredSandboxAccess.owner_object_id == identity.object_id,
                    StoredSandboxMember.member_object_id == identity.object_id,
                )
            )
        result = await self.db_session.execute(statement)
        return result.scalars().first()
