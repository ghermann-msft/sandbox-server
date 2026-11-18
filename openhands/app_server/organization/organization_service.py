from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversationStartRequest,
)
from openhands.app_server.errors import AuthError, PermissionsError
from openhands.app_server.organization.organization_models import (
    ORGANIZATION_ID_HEADER,
    AuthenticationIdentity,
    OrganizationKind,
    OrganizationListResponse,
    OrganizationMember,
    OrganizationMemberIdentity,
    OrganizationMemberListResponse,
    OrganizationMembershipResponse,
    OrganizationPermission,
    OrganizationRole,
    OrganizationSummary,
    StoredMembership,
    StoredOrganization,
    StoredPermission,
    StoredRole,
    StoredRolePermission,
    StoredUser,
)
from openhands.app_server.user.user_context import UserContext


@dataclass
class OrganizationService:
    db_session: AsyncSession
    user_context: UserContext

    async def _identity(self) -> AuthenticationIdentity:
        identity = await self.user_context.get_authentication_identity()
        if identity is None:
            raise AuthError('Authentication is required')
        return identity

    async def _user(self) -> StoredUser:
        identity = await self._identity()
        tenant_id = identity.tenant_id or ''
        query = select(StoredUser).where(
            StoredUser.identity_provider == identity.provider,
            StoredUser.tenant_id == tenant_id,
            StoredUser.subject_id == identity.subject_id,
        )
        user = await self.db_session.scalar(query)
        if user is None:
            user = StoredUser(
                identity_provider=identity.provider,
                tenant_id=tenant_id,
                subject_id=identity.subject_id,
            )
            self.db_session.add(user)
            await self.db_session.flush()
        return user

    async def _role(self, role: OrganizationRole) -> StoredRole:
        stored = await self.db_session.scalar(
            select(StoredRole).where(StoredRole.name == role.value)
        )
        if stored is None:
            raise RuntimeError(f'RBAC role is not seeded: {role.value}')
        return stored

    async def _ensure_personal_organization(
        self, user: StoredUser
    ) -> StoredOrganization:
        organization = await self.db_session.scalar(
            select(StoredOrganization).where(
                StoredOrganization.personal_owner_user_id == user.id
            )
        )
        if organization is not None:
            return organization

        organization = StoredOrganization(
            name='Personal',
            kind=OrganizationKind.PERSONAL.value,
            personal_owner_user_id=user.id,
            is_visible=True,
        )
        owner_role = await self._role(OrganizationRole.OWNER)
        self.db_session.add(organization)
        await self.db_session.flush()
        self.db_session.add(
            StoredMembership(
                organization_id=organization.id,
                user_id=user.id,
                role_id=owner_role.id,
            )
        )
        await self.db_session.flush()
        return organization

    async def resolve_organization_id(
        self,
        requested_org_id: UUID | None = None,
        permission: OrganizationPermission = OrganizationPermission.ORG_VIEW,
    ) -> UUID:
        user = await self._user()
        personal = await self._ensure_personal_organization(user)
        if requested_org_id is None:
            requested_org_id = await self.user_context.get_effective_org_id()
        organization_id = requested_org_id or personal.id
        await self.require_permission(organization_id, permission, user=user)
        return organization_id

    async def require_permission(
        self,
        organization_id: UUID,
        permission: OrganizationPermission,
        *,
        user: StoredUser | None = None,
    ) -> OrganizationMembershipResponse:
        user = user or await self._user()
        query = (
            select(StoredMembership, StoredRole)
            .join(StoredRole, StoredRole.id == StoredMembership.role_id)
            .where(
                StoredMembership.organization_id == organization_id,
                StoredMembership.user_id == user.id,
            )
        )
        row = (await self.db_session.execute(query)).one_or_none()
        if row is None:
            raise PermissionsError('Not a member of the requested organization')
        _membership, role = row
        permissions = list(
            (
                await self.db_session.scalars(
                    select(StoredPermission.name)
                    .join(
                        StoredRolePermission,
                        StoredRolePermission.permission_id == StoredPermission.id,
                    )
                    .where(StoredRolePermission.role_id == role.id)
                    .order_by(StoredPermission.name)
                )
            ).all()
        )
        if permission.value not in permissions:
            raise PermissionsError(
                f'Missing organization permission: {permission.value}'
            )
        return OrganizationMembershipResponse(
            org_id=organization_id,
            user_id=user.id,
            role=OrganizationRole(role.name),
            permissions=[OrganizationPermission(value) for value in permissions],
        )

    async def list_organizations(
        self, requested_org_id: UUID | None = None
    ) -> OrganizationListResponse:
        user = await self._user()
        personal = await self._ensure_personal_organization(user)
        query = (
            select(StoredOrganization)
            .join(
                StoredMembership,
                StoredMembership.organization_id == StoredOrganization.id,
            )
            .where(StoredMembership.user_id == user.id)
            .order_by(
                StoredOrganization.personal_owner_user_id.is_(None),
                StoredOrganization.name,
            )
        )
        organizations = list((await self.db_session.scalars(query)).all())
        current_org_id = await self.resolve_organization_id(requested_org_id)
        return OrganizationListResponse(
            items=[
                OrganizationSummary(
                    id=organization.id,
                    name=organization.name,
                    is_personal=organization.id == personal.id,
                    is_visible=organization.is_visible,
                )
                for organization in organizations
            ],
            current_org_id=current_org_id,
        )

    async def get_membership(
        self, organization_id: UUID
    ) -> OrganizationMembershipResponse:
        return await self.require_permission(
            organization_id, OrganizationPermission.ORG_VIEW
        )

    async def list_members(
        self, organization_id: UUID
    ) -> OrganizationMemberListResponse:
        await self.require_permission(organization_id, OrganizationPermission.ORG_VIEW)
        rows = (
            await self.db_session.execute(
                select(StoredMembership, StoredUser, StoredRole)
                .join(StoredUser, StoredUser.id == StoredMembership.user_id)
                .join(StoredRole, StoredRole.id == StoredMembership.role_id)
                .where(StoredMembership.organization_id == organization_id)
                .order_by(StoredRole.name, StoredUser.subject_id)
            )
        ).all()
        return OrganizationMemberListResponse(
            items=[
                self._member_response(membership, user, role)
                for membership, user, role in rows
            ]
        )

    async def update_member_role(
        self,
        organization_id: UUID,
        user_id: UUID,
        role: OrganizationRole,
    ) -> OrganizationMember:
        await self.require_permission(
            organization_id, OrganizationPermission.ORG_MANAGE_MEMBERS
        )
        organization = await self.db_session.get(StoredOrganization, organization_id)
        if organization is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Organization not found',
            )
        if organization.kind == OrganizationKind.PERSONAL.value:
            raise PermissionsError('Personal organization roles cannot be changed')

        rows = (
            await self.db_session.execute(
                select(StoredMembership, StoredUser, StoredRole)
                .join(StoredUser, StoredUser.id == StoredMembership.user_id)
                .join(StoredRole, StoredRole.id == StoredMembership.role_id)
                .where(StoredMembership.organization_id == organization_id)
                .order_by(StoredMembership.id)
                .with_for_update(of=StoredMembership)
            )
        ).all()
        target = next(
            (row for row in rows if row[0].user_id == user_id),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Organization member not found',
            )

        membership, user, current_role = target
        if (
            current_role.name == OrganizationRole.OWNER.value
            and role != OrganizationRole.OWNER
            and sum(
                item_role.name == OrganizationRole.OWNER.value
                for _, _, item_role in rows
            )
            == 1
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='An organization must have at least one owner',
            )

        new_role = await self._role(role)
        membership.role_id = new_role.id
        await self.db_session.flush()
        return self._member_response(membership, user, new_role)

    @staticmethod
    def _member_response(
        membership: StoredMembership,
        user: StoredUser,
        role: StoredRole,
    ) -> OrganizationMember:
        return OrganizationMember(
            user_id=membership.user_id,
            identity=OrganizationMemberIdentity(
                provider=user.identity_provider,
                tenant_id=user.tenant_id or None,
                subject_id=user.subject_id,
            ),
            role=OrganizationRole(role.name),
        )


async def bind_conversation_organization(
    start_request: AppConversationStartRequest,
    requested_organization_id: UUID | None,
    db_session: AsyncSession,
    user_context: UserContext,
) -> None:
    """Validate a browser-requested tenant against durable RBAC."""

    identity = await user_context.get_authentication_identity()
    if identity is None:
        if start_request.organization_id is not None:
            raise PermissionsError('Authenticated organization selection is required')
        return
    if requested_organization_id is None:
        raise PermissionsError(f'{ORGANIZATION_ID_HEADER} is required')
    if (
        start_request.organization_id is not None
        and start_request.organization_id != requested_organization_id
    ):
        raise PermissionsError(
            f'Request organization does not match {ORGANIZATION_ID_HEADER}'
        )
    start_request.organization_id = await OrganizationService(
        db_session, user_context
    ).resolve_organization_id(
        requested_organization_id,
        OrganizationPermission.CONVERSATION_CREATE,
    )


class BootstrapMember(BaseModel):
    model_config = ConfigDict(extra='forbid')

    provider: str = 'entra'
    tenant_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    role: OrganizationRole


class BootstrapOrganization(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: UUID
    name: str = Field(min_length=1)
    kind: Literal['shared']
    is_visible: bool = True
    members: list[BootstrapMember] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_unique_members(self) -> BootstrapOrganization:
        identities = [
            (member.provider, member.tenant_id, member.subject_id)
            for member in self.members
        ]
        if len(identities) != len(set(identities)):
            raise ValueError('Duplicate organization bootstrap member')
        return self


class BootstrapConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    organizations: list[BootstrapOrganization]

    @model_validator(mode='after')
    def validate_unique_organizations(self) -> BootstrapConfig:
        organization_ids = [organization.id for organization in self.organizations]
        if len(organization_ids) != len(set(organization_ids)):
            raise ValueError('Duplicate bootstrap organization ID')
        return self


def _bootstrap_config_from_env() -> BootstrapConfig | None:
    inline = os.getenv('OH_RBAC_BOOTSTRAP_JSON')
    file_name = os.getenv('OH_RBAC_BOOTSTRAP_JSON_FILE')
    if inline and file_name:
        raise ValueError(
            'Configure only one of OH_RBAC_BOOTSTRAP_JSON or '
            'OH_RBAC_BOOTSTRAP_JSON_FILE'
        )
    if file_name:
        inline = Path(file_name).read_text(encoding='utf-8')
    if inline:
        return BootstrapConfig.model_validate_json(inline)

    values = {
        'organization_id': os.getenv('OH_RBAC_BOOTSTRAP_ORGANIZATION_ID'),
        'organization_name': os.getenv('OH_RBAC_BOOTSTRAP_ORGANIZATION_NAME'),
        'owner_provider': os.getenv('OH_RBAC_BOOTSTRAP_OWNER_PROVIDER'),
        'owner_tenant_id': os.getenv('OH_RBAC_BOOTSTRAP_OWNER_TENANT_ID'),
        'owner_object_id': os.getenv('OH_RBAC_BOOTSTRAP_OWNER_OBJECT_ID'),
    }
    if not any(values.values()):
        return None
    required = ('organization_id', 'owner_tenant_id', 'owner_object_id')
    missing = [key for key in required if not values[key]]
    if missing:
        raise ValueError(
            'Incomplete RBAC bootstrap configuration; missing ' + ', '.join(missing)
        )

    return BootstrapConfig(
        organizations=[
            BootstrapOrganization(
                id=UUID(values['organization_id'] or ''),
                name=values['organization_name'] or 'Halo Studios',
                kind='shared',
                members=[
                    BootstrapMember(
                        provider=values['owner_provider'] or 'entra',
                        tenant_id=values['owner_tenant_id'] or '',
                        subject_id=values['owner_object_id'] or '',
                        role=OrganizationRole.OWNER,
                    )
                ],
            )
        ]
    )


def bootstrap_organization_from_env(engine: Engine) -> None:
    """Idempotently seed shared organizations and immutable identity memberships."""

    config = _bootstrap_config_from_env()
    if config is None:
        return

    with Session(engine) as session:
        roles: dict[OrganizationRole, StoredRole] = {
            OrganizationRole(role.name): role
            for role in session.scalars(select(StoredRole)).all()
        }
        missing_roles: set[OrganizationRole] = set(OrganizationRole).difference(roles)
        if missing_roles:
            raise RuntimeError(
                'RBAC roles are not seeded: '
                + ', '.join(sorted(role.value for role in missing_roles))
            )

        for organization_seed in config.organizations:
            organization = session.get(StoredOrganization, organization_seed.id)
            if organization is None:
                organization = StoredOrganization(
                    id=organization_seed.id,
                    name=organization_seed.name,
                    kind=OrganizationKind.SHARED.value,
                    is_visible=organization_seed.is_visible,
                )
                session.add(organization)
                session.flush()
            elif organization.kind != OrganizationKind.SHARED.value:
                raise ValueError(
                    f'Bootstrap organization {organization_seed.id} is not shared'
                )

            for member in organization_seed.members:
                user = session.scalar(
                    select(StoredUser).where(
                        StoredUser.identity_provider == member.provider,
                        StoredUser.tenant_id == member.tenant_id,
                        StoredUser.subject_id == member.subject_id,
                    )
                )
                if user is None:
                    user = StoredUser(
                        identity_provider=member.provider,
                        tenant_id=member.tenant_id,
                        subject_id=member.subject_id,
                    )
                    session.add(user)
                    session.flush()

                membership = session.scalar(
                    select(StoredMembership).where(
                        StoredMembership.organization_id == organization.id,
                        StoredMembership.user_id == user.id,
                    )
                )
                if membership is None:
                    session.add(
                        StoredMembership(
                            organization_id=organization.id,
                            user_id=user.id,
                            role_id=roles[member.role].id,
                        )
                    )
        session.commit()
