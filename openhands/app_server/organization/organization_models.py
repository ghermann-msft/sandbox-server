from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from openhands.agent_server.utils import utc_now
from openhands.app_server.utils.sql_utils import Base

ORGANIZATION_ID_HEADER = 'X-Org-Id'


class OrganizationKind(StrEnum):
    PERSONAL = 'personal'
    SHARED = 'shared'


class OrganizationRole(StrEnum):
    OWNER = 'owner'
    ADMIN = 'admin'
    MEMBER = 'member'


class OrganizationPermission(StrEnum):
    ORG_VIEW = 'organization:view'
    ORG_MANAGE = 'organization:manage'
    ORG_MANAGE_MEMBERS = 'organization:manage_members'
    CONVERSATION_CREATE = 'conversation:create'
    CONVERSATION_READ = 'conversation:read'
    CONVERSATION_WRITE = 'conversation:write'
    HINDSIGHT_RECALL = 'hindsight:recall'
    HINDSIGHT_RETAIN = 'hindsight:retain'


class AuthenticationIdentity(BaseModel):
    """Immutable identity asserted by the configured authentication provider."""

    provider: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    tenant_id: str | None = None


class StoredUser(Base):
    __tablename__ = 'rbac_users'
    __table_args__ = (
        UniqueConstraint(
            'identity_provider',
            'tenant_id',
            'subject_id',
            name='uq_rbac_users_identity',
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    identity_provider: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default='')
    subject_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class StoredOrganization(Base):
    __tablename__ = 'organizations'

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    personal_owner_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey('rbac_users.id', ondelete='CASCADE'),
        nullable=True,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class StoredRole(Base):
    __tablename__ = 'organization_roles'

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)


class StoredPermission(Base):
    __tablename__ = 'organization_permissions'

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)


class StoredRolePermission(Base):
    __tablename__ = 'organization_role_permissions'

    role_id: Mapped[UUID] = mapped_column(
        ForeignKey('organization_roles.id', ondelete='CASCADE'), primary_key=True
    )
    permission_id: Mapped[UUID] = mapped_column(
        ForeignKey('organization_permissions.id', ondelete='CASCADE'),
        primary_key=True,
    )


class StoredMembership(Base):
    __tablename__ = 'organization_memberships'
    __table_args__ = (
        UniqueConstraint(
            'organization_id',
            'user_id',
            name='uq_organization_membership',
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey('organizations.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey('rbac_users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    role_id: Mapped[UUID] = mapped_column(
        ForeignKey('organization_roles.id'),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class OrganizationSummary(BaseModel):
    id: UUID
    name: str
    is_personal: bool = False
    is_visible: bool = True


class OrganizationListResponse(BaseModel):
    items: list[OrganizationSummary]
    current_org_id: UUID


class OrganizationMembershipResponse(BaseModel):
    org_id: UUID
    user_id: UUID
    role: OrganizationRole
    permissions: list[OrganizationPermission]
