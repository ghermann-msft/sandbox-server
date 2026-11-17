from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from openhands.app_server.errors import AuthError, PermissionsError
from openhands.app_server.organization.organization_models import (
    AuthenticationIdentity,
    OrganizationKind,
    OrganizationListResponse,
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


def bootstrap_organization_from_env(engine: Engine) -> None:
    """Seed one shared organization and owner from deployment configuration."""

    values = {
        'organization_id': os.getenv('OH_RBAC_BOOTSTRAP_ORGANIZATION_ID'),
        'organization_name': os.getenv('OH_RBAC_BOOTSTRAP_ORGANIZATION_NAME'),
        'owner_provider': os.getenv('OH_RBAC_BOOTSTRAP_OWNER_PROVIDER'),
        'owner_tenant_id': os.getenv('OH_RBAC_BOOTSTRAP_OWNER_TENANT_ID'),
        'owner_object_id': os.getenv('OH_RBAC_BOOTSTRAP_OWNER_OBJECT_ID'),
    }
    if not any(values.values()):
        return
    required = ('organization_id', 'owner_tenant_id', 'owner_object_id')
    missing = [key for key in required if not values[key]]
    if missing:
        raise ValueError(
            'Incomplete RBAC bootstrap configuration; missing ' + ', '.join(missing)
        )

    organization_id = UUID(values['organization_id'] or '')
    with Session(engine) as session:
        user = session.scalar(
            select(StoredUser).where(
                StoredUser.identity_provider == (values['owner_provider'] or 'entra'),
                StoredUser.tenant_id == values['owner_tenant_id'],
                StoredUser.subject_id == values['owner_object_id'],
            )
        )
        if user is None:
            user = StoredUser(
                identity_provider=values['owner_provider'] or 'entra',
                tenant_id=values['owner_tenant_id'] or '',
                subject_id=values['owner_object_id'] or '',
            )
            session.add(user)
            session.flush()

        organization = session.get(StoredOrganization, organization_id)
        if organization is None:
            organization = StoredOrganization(
                id=organization_id,
                name=values['organization_name'] or 'Halo Studios',
                kind=OrganizationKind.SHARED.value,
                is_visible=True,
            )
            session.add(organization)
            session.flush()

        owner_role = session.scalar(
            select(StoredRole).where(StoredRole.name == OrganizationRole.OWNER.value)
        )
        if owner_role is None:
            raise RuntimeError('RBAC owner role is not seeded')
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
                    role_id=owner_role.id,
                )
            )
        else:
            membership.role_id = owner_role.id
        session.commit()
