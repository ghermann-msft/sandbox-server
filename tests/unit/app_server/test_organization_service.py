from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from openhands.app_server.errors import PermissionsError
from openhands.app_server.organization.organization_models import (
    AuthenticationIdentity,
    OrganizationKind,
    OrganizationPermission,
    OrganizationRole,
    OrganizationRoleUpdate,
    StoredMembership,
    StoredOrganization,
    StoredPermission,
    StoredRole,
    StoredRolePermission,
    StoredUser,
)
from openhands.app_server.organization.organization_service import (
    OrganizationService,
    bootstrap_organization_from_env,
)
from openhands.app_server.user.specifiy_user_context import SpecifyUserContext
from openhands.app_server.utils.sql_utils import Base


@dataclass(frozen=True)
class IdentityUserContext(SpecifyUserContext):
    identity: AuthenticationIdentity
    effective_org_id: UUID | None = None

    async def get_authentication_identity(self) -> AuthenticationIdentity:
        return self.identity

    async def get_effective_org_id(self) -> UUID | None:
        return self.effective_org_id


async def _seed_roles(db_session: AsyncSession) -> dict[str, StoredRole]:
    roles = {
        role.value: StoredRole(id=uuid4(), name=role.value) for role in OrganizationRole
    }
    permissions = {
        permission.value: StoredPermission(id=uuid4(), name=permission.value)
        for permission in OrganizationPermission
    }
    db_session.add_all([*roles.values(), *permissions.values()])
    await db_session.flush()
    assignments = {
        OrganizationRole.OWNER.value: set(permissions),
        OrganizationRole.ADMIN.value: set(permissions)
        - {OrganizationPermission.ORG_MANAGE.value},
        OrganizationRole.MEMBER.value: {
            OrganizationPermission.ORG_VIEW.value,
            OrganizationPermission.CONVERSATION_CREATE.value,
            OrganizationPermission.CONVERSATION_READ.value,
            OrganizationPermission.CONVERSATION_WRITE.value,
            OrganizationPermission.HINDSIGHT_RECALL.value,
            OrganizationPermission.HINDSIGHT_RETAIN.value,
        },
    }
    db_session.add_all(
        [
            StoredRolePermission(
                role_id=roles[role].id,
                permission_id=permissions[permission].id,
            )
            for role, role_permissions in assignments.items()
            for permission in role_permissions
        ]
    )
    await db_session.flush()
    return roles


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        'sqlite+aiosqlite:///:memory:',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_maker() as session:
        await _seed_roles(session)
        yield session
    await engine.dispose()


def _context(
    subject_id: str, *, effective_org_id: UUID | None = None
) -> IdentityUserContext:
    return IdentityUserContext(
        user_id=subject_id,
        identity=AuthenticationIdentity(
            provider='entra',
            tenant_id='tenant-1',
            subject_id=subject_id,
        ),
        effective_org_id=effective_org_id,
    )


async def _shared_organization(
    db_session: AsyncSession,
    members: list[tuple[str, OrganizationRole]],
) -> tuple[StoredOrganization, dict[str, StoredUser]]:
    users = {}
    for subject_id, _ in members:
        await OrganizationService(db_session, _context(subject_id)).list_organizations()
        user = await db_session.scalar(
            select(StoredUser).where(StoredUser.subject_id == subject_id)
        )
        assert user is not None
        users[subject_id] = user

    organization = StoredOrganization(
        id=uuid4(),
        name='Halo Studios',
        kind=OrganizationKind.SHARED.value,
        is_visible=True,
    )
    db_session.add(organization)
    await db_session.flush()
    for subject_id, role in members:
        stored_role = await db_session.scalar(
            select(StoredRole).where(StoredRole.name == role.value)
        )
        assert stored_role is not None
        db_session.add(
            StoredMembership(
                organization_id=organization.id,
                user_id=users[subject_id].id,
                role_id=stored_role.id,
            )
        )
    await db_session.flush()
    return organization, users


@pytest.mark.asyncio
async def test_personal_and_shared_organization_list_and_current_org(db_session):
    owner_context = _context('owner-object')
    service = OrganizationService(db_session, owner_context)
    personal = await service.list_organizations()
    owner = await db_session.scalar(
        select(StoredUser).where(StoredUser.subject_id == 'owner-object')
    )
    owner_role = await db_session.scalar(
        select(StoredRole).where(StoredRole.name == OrganizationRole.OWNER.value)
    )
    assert owner is not None
    assert owner_role is not None

    halo_id = uuid4()
    db_session.add(
        StoredOrganization(
            id=halo_id,
            name='Halo Studios',
            kind=OrganizationKind.SHARED.value,
            is_visible=True,
        )
    )
    await db_session.flush()
    db_session.add(
        StoredMembership(
            organization_id=halo_id,
            user_id=owner.id,
            role_id=owner_role.id,
        )
    )
    await db_session.flush()

    response = await OrganizationService(
        db_session, _context('owner-object', effective_org_id=halo_id)
    ).list_organizations()

    assert response.model_dump(mode='json') == {
        'items': [
            {
                'id': str(personal.items[0].id),
                'name': 'Personal',
                'is_personal': True,
                'is_visible': True,
            },
            {
                'id': str(halo_id),
                'name': 'Halo Studios',
                'is_personal': False,
                'is_visible': True,
            },
        ],
        'current_org_id': str(halo_id),
    }


@pytest.mark.asyncio
async def test_requested_organization_cannot_forge_membership(db_session):
    owner_service = OrganizationService(db_session, _context('owner-object'))
    await owner_service.list_organizations()
    owner = await db_session.scalar(
        select(StoredUser).where(StoredUser.subject_id == 'owner-object')
    )
    owner_role = await db_session.scalar(
        select(StoredRole).where(StoredRole.name == OrganizationRole.OWNER.value)
    )
    assert owner is not None
    assert owner_role is not None
    halo = StoredOrganization(
        id=uuid4(),
        name='Halo Studios',
        kind=OrganizationKind.SHARED.value,
        is_visible=True,
    )
    db_session.add(halo)
    await db_session.flush()
    db_session.add(
        StoredMembership(
            organization_id=halo.id,
            user_id=owner.id,
            role_id=owner_role.id,
        )
    )
    await db_session.flush()

    attacker_service = OrganizationService(db_session, _context('other-object'))
    with pytest.raises(PermissionsError, match='Not a member'):
        await attacker_service.resolve_organization_id(
            halo.id, OrganizationPermission.HINDSIGHT_RECALL
        )


@pytest.mark.asyncio
async def test_member_hindsight_allow_and_org_manage_deny(db_session):
    service = OrganizationService(db_session, _context('member-object'))
    await service.list_organizations()
    user = await db_session.scalar(
        select(StoredUser).where(StoredUser.subject_id == 'member-object')
    )
    member_role = await db_session.scalar(
        select(StoredRole).where(StoredRole.name == OrganizationRole.MEMBER.value)
    )
    assert user is not None
    assert member_role is not None
    halo = StoredOrganization(
        id=uuid4(),
        name='Halo Studios',
        kind=OrganizationKind.SHARED.value,
        is_visible=True,
    )
    db_session.add(halo)
    await db_session.flush()
    db_session.add(
        StoredMembership(
            organization_id=halo.id,
            user_id=user.id,
            role_id=member_role.id,
        )
    )
    await db_session.flush()

    membership = await service.require_permission(
        halo.id, OrganizationPermission.HINDSIGHT_RETAIN
    )
    assert membership.role == OrganizationRole.MEMBER
    assert OrganizationPermission.HINDSIGHT_RECALL in membership.permissions

    with pytest.raises(PermissionsError, match='organization:manage'):
        await service.require_permission(halo.id, OrganizationPermission.ORG_MANAGE)


@pytest.mark.asyncio
async def test_member_can_list_members_with_immutable_identities(db_session):
    organization, users = await _shared_organization(
        db_session,
        [
            ('owner-object', OrganizationRole.OWNER),
            ('member-object', OrganizationRole.MEMBER),
        ],
    )

    response = await OrganizationService(
        db_session, _context('member-object')
    ).list_members(organization.id)

    assert {item.user_id for item in response.items} == set(
        user.id for user in users.values()
    )
    member = next(
        item for item in response.items if item.identity.subject_id == 'member-object'
    )
    assert member.identity.model_dump() == {
        'provider': 'entra',
        'tenant_id': 'tenant-1',
        'subject_id': 'member-object',
    }
    assert member.role == OrganizationRole.MEMBER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('actor', 'actor_role', 'new_role'),
    [
        ('owner-object', OrganizationRole.OWNER, OrganizationRole.ADMIN),
        ('admin-object', OrganizationRole.ADMIN, OrganizationRole.OWNER),
    ],
)
async def test_owner_and_admin_can_update_member_roles(
    db_session,
    actor,
    actor_role,
    new_role,
):
    organization, users = await _shared_organization(
        db_session,
        [
            (actor, actor_role),
            ('existing-owner', OrganizationRole.OWNER),
            ('member-object', OrganizationRole.MEMBER),
        ],
    )

    updated = await OrganizationService(db_session, _context(actor)).update_member_role(
        organization.id,
        users['member-object'].id,
        new_role,
    )

    assert updated.user_id == users['member-object'].id
    assert updated.role == new_role
    assert updated.identity.subject_id == 'member-object'


@pytest.mark.asyncio
async def test_member_cannot_update_roles(db_session):
    organization, users = await _shared_organization(
        db_session,
        [
            ('owner-object', OrganizationRole.OWNER),
            ('member-object', OrganizationRole.MEMBER),
        ],
    )

    with pytest.raises(PermissionsError, match='organization:manage_members'):
        await OrganizationService(
            db_session, _context('member-object')
        ).update_member_role(
            organization.id,
            users['owner-object'].id,
            OrganizationRole.ADMIN,
        )


@pytest.mark.asyncio
async def test_personal_organization_role_is_immutable(db_session):
    service = OrganizationService(db_session, _context('owner-object'))
    organizations = await service.list_organizations()
    owner = await db_session.scalar(
        select(StoredUser).where(StoredUser.subject_id == 'owner-object')
    )
    assert owner is not None

    with pytest.raises(PermissionsError, match='Personal organization'):
        await service.update_member_role(
            organizations.current_org_id,
            owner.id,
            OrganizationRole.MEMBER,
        )


@pytest.mark.asyncio
async def test_last_owner_cannot_be_demoted(db_session):
    organization, users = await _shared_organization(
        db_session,
        [
            ('owner-object', OrganizationRole.OWNER),
            ('member-object', OrganizationRole.MEMBER),
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        await OrganizationService(
            db_session, _context('owner-object')
        ).update_member_role(
            organization.id,
            users['owner-object'].id,
            OrganizationRole.ADMIN,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == 'An organization must have at least one owner'


def test_role_update_rejects_unknown_role():
    with pytest.raises(ValidationError):
        OrganizationRoleUpdate.model_validate({'role': 'superuser'})


def test_bootstrap_shared_organization_owner(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "rbac.db"}')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        roles = {
            role.value: StoredRole(id=uuid4(), name=role.value)
            for role in OrganizationRole
        }
        session.add_all(roles.values())
        session.commit()

    halo_id = uuid4()
    monkeypatch.setenv('OH_RBAC_BOOTSTRAP_ORGANIZATION_ID', str(halo_id))
    monkeypatch.setenv('OH_RBAC_BOOTSTRAP_ORGANIZATION_NAME', 'Halo Studios')
    monkeypatch.setenv('OH_RBAC_BOOTSTRAP_OWNER_PROVIDER', 'entra')
    monkeypatch.setenv('OH_RBAC_BOOTSTRAP_OWNER_TENANT_ID', 'tenant-1')
    monkeypatch.setenv('OH_RBAC_BOOTSTRAP_OWNER_OBJECT_ID', 'owner-object')

    bootstrap_organization_from_env(engine)
    bootstrap_organization_from_env(engine)

    with Session(engine) as session:
        organization = session.get(StoredOrganization, halo_id)
        memberships = session.scalars(
            select(StoredMembership).where(StoredMembership.organization_id == halo_id)
        ).all()
        assert organization is not None
        assert organization.name == 'Halo Studios'
        assert len(memberships) == 1
