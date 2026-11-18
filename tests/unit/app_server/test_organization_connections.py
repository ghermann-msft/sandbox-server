from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from openhands.app_server.errors import PermissionsError
from openhands.app_server.integrations.provider import ProviderToken
from openhands.app_server.organization.connection_models import (
    MCPConnectionUpsert,
    OrganizationConnectionKind,
    StoredOrganizationConnection,
)
from openhands.app_server.organization.connection_service import (
    OrganizationConnectionService,
)
from openhands.app_server.organization.organization_models import (
    AuthenticationIdentity,
    OrganizationKind,
    OrganizationPermission,
    OrganizationRole,
    StoredMembership,
    StoredOrganization,
    StoredPermission,
    StoredRole,
    StoredRolePermission,
    StoredUser,
)
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.organization.profile_models import (
    OrganizationProfileSaveRequest,
    StoredOrganizationProfiles,
)
from openhands.app_server.organization.profile_service import OrganizationProfileService
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.settings.llm_profiles import LLMProfiles
from openhands.app_server.settings.settings_models import Settings
from openhands.app_server.settings.sql_settings_store import StoredUserSettings
from openhands.app_server.user.auth_user_context import AuthUserContext
from openhands.app_server.user.specifiy_user_context import SpecifyUserContext
from openhands.app_server.utils.encryption_key import EncryptionKey
from openhands.app_server.utils.sql_utils import Base
from openhands.sdk.mcp.config import MCPServer


@dataclass(frozen=True)
class IdentityUserContext(SpecifyUserContext):
    identity: AuthenticationIdentity
    effective_org_id: UUID | None = None

    async def get_authentication_identity(self) -> AuthenticationIdentity:
        return self.identity

    async def get_effective_org_id(self) -> UUID | None:
        return self.effective_org_id


def _context(subject_id: str, organization_id: UUID | None = None):
    return IdentityUserContext(
        user_id=f'entra:tenant-1:{subject_id}',
        identity=AuthenticationIdentity(
            provider='entra',
            tenant_id='tenant-1',
            subject_id=subject_id,
        ),
        effective_org_id=organization_id,
    )


def _jwt_service() -> JwtService:
    return JwtService([EncryptionKey(id='test', key=SecretStr('connection-key'))])


async def _seed_roles(db_session: AsyncSession) -> dict[OrganizationRole, StoredRole]:
    roles = {role: StoredRole(id=uuid4(), name=role.value) for role in OrganizationRole}
    permissions = {
        permission: StoredPermission(id=uuid4(), name=permission.value)
        for permission in OrganizationPermission
    }
    db_session.add_all([*roles.values(), *permissions.values()])
    await db_session.flush()
    assignments = {
        OrganizationRole.OWNER: set(OrganizationPermission),
        OrganizationRole.ADMIN: set(OrganizationPermission)
        - {OrganizationPermission.ORG_MANAGE},
        OrganizationRole.MEMBER: {
            OrganizationPermission.ORG_VIEW,
            OrganizationPermission.CONVERSATION_CREATE,
            OrganizationPermission.CONVERSATION_READ,
            OrganizationPermission.CONVERSATION_WRITE,
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


async def _add_shared_membership(
    db_session: AsyncSession,
    subject_id: str,
    role: OrganizationRole,
    organization_id: UUID | None = None,
) -> UUID:
    context = _context(subject_id)
    await OrganizationService(db_session, context).list_organizations()
    user = await db_session.scalar(
        select(StoredUser).where(StoredUser.subject_id == subject_id)
    )
    stored_role = await db_session.scalar(
        select(StoredRole).where(StoredRole.name == role.value)
    )
    assert user is not None
    assert stored_role is not None
    organization_id = organization_id or uuid4()
    organization = await db_session.get(StoredOrganization, organization_id)
    if organization is None:
        db_session.add(
            StoredOrganization(
                id=organization_id,
                name='Halo Studios',
                kind=OrganizationKind.SHARED.value,
                is_visible=True,
            )
        )
        await db_session.flush()
    db_session.add(
        StoredMembership(
            organization_id=organization_id,
            user_id=user.id,
            role_id=stored_role.id,
        )
    )
    await db_session.flush()
    return organization_id


def _atlassian_server(secret: str) -> MCPServer:
    return MCPServer.model_validate(
        {
            'url': 'https://mcp.atlassian.example/mcp',
            'headers': {'Authorization': f'Bearer {secret}'},
        }
    )


def _perforce_server(secret: str) -> MCPServer:
    return MCPServer.model_validate(
        {
            'command': 'p4-mcp-server',
            'env': {
                'P4PORT': 'ssl:perforce.example:1666',
                'P4USER': 'openhands',
                'P4PASSWD': secret,
            },
        }
    )


@pytest.mark.asyncio
async def test_owner_and_admin_manage_connections_member_reads_only(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    await _add_shared_membership(
        db_session, 'admin', OrganizationRole.ADMIN, organization_id
    )
    await _add_shared_membership(
        db_session, 'member', OrganizationRole.MEMBER, organization_id
    )
    jwt_service = _jwt_service()

    owner = OrganizationConnectionService(
        db_session, _context('owner', organization_id), jwt_service
    )
    await owner.store_mcp(
        organization_id,
        OrganizationConnectionKind.ATLASSIAN,
        MCPConnectionUpsert(server=_atlassian_server('owner-secret')),
    )
    await owner.store_github_token(
        organization_id,
        ProviderToken(
            token=SecretStr('github-oauth-token'),
            user_id='123',
            host='github.com',
        ),
    )
    admin = OrganizationConnectionService(
        db_session, _context('admin', organization_id), jwt_service
    )
    await admin.store_mcp(
        organization_id,
        OrganizationConnectionKind.PERFORCE,
        MCPConnectionUpsert(server=_perforce_server('admin-secret')),
    )

    member = OrganizationConnectionService(
        db_session, _context('member', organization_id), jwt_service
    )
    response = await member.list(organization_id)
    assert all(item.connected for item in response.items if item.kind != 'github')
    assert all(not item.editable for item in response.items)
    assert 'secret' not in response.model_dump_json()
    consumed = await member.mcp_servers(organization_id)
    exposed = consumed['perforce'].model_dump_json(context={'expose_secrets': True})
    assert 'admin-secret' in exposed
    github = await member.github_token(organization_id)
    assert github is not None
    assert github.token.get_secret_value() == 'github-oauth-token'

    with pytest.raises(PermissionsError, match='required organization permissions'):
        await member.store_mcp(
            organization_id,
            OrganizationConnectionKind.PERFORCE,
            MCPConnectionUpsert(server=_perforce_server('replacement')),
        )

    other_organization_id = await _add_shared_membership(
        db_session, 'other-owner', OrganizationRole.OWNER
    )
    with pytest.raises(PermissionsError, match='Not a member'):
        await owner.list(other_organization_id)


@pytest.mark.asyncio
async def test_payload_is_encrypted_and_update_delete_are_scoped(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    service = OrganizationConnectionService(
        db_session, _context('owner', organization_id), _jwt_service()
    )
    await service.store_mcp(
        organization_id,
        OrganizationConnectionKind.PERFORCE,
        MCPConnectionUpsert(server=_perforce_server('first-secret')),
    )
    row = await db_session.get(
        StoredOrganizationConnection,
        (organization_id, OrganizationConnectionKind.PERFORCE.value),
    )
    assert row is not None
    assert 'first-secret' not in row.encrypted_payload

    await service.store_mcp(
        organization_id,
        OrganizationConnectionKind.PERFORCE,
        MCPConnectionUpsert(server=_perforce_server('second-secret')),
    )
    assert row.revision == 1
    assert 'second-secret' in (await service.mcp_servers(organization_id))[
        'perforce'
    ].model_dump_json(context={'expose_secrets': True})

    await service.delete(organization_id, OrganizationConnectionKind.PERFORCE)
    assert (
        await db_session.get(
            StoredOrganizationConnection,
            (organization_id, OrganizationConnectionKind.PERFORCE.value),
        )
        is None
    )


@pytest.mark.asyncio
async def test_personal_connection_remains_isolated_from_shared_connection(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    context = _context('owner', organization_id)
    user_id = await context.get_user_id()
    personal = Settings()
    personal.agent_settings = personal.agent_settings.model_copy(
        update={'mcp_config': {'perforce': _perforce_server('personal-secret')}}
    )
    jwt_service = _jwt_service()
    db_session.add(
        StoredUserSettings(
            user_id=user_id,
            encrypted_payload=jwt_service.encrypt_value(
                personal.model_dump_json(
                    context={'expose_secrets': True, 'persist_settings': True}
                )
            ),
        )
    )
    await db_session.flush()

    service = OrganizationConnectionService(db_session, context, jwt_service)
    await service.store_mcp(
        organization_id,
        OrganizationConnectionKind.PERFORCE,
        MCPConnectionUpsert(server=_perforce_server('shared-secret')),
    )
    shared = (await service.mcp_servers(organization_id))['perforce']
    assert 'shared-secret' in shared.model_dump_json(context={'expose_secrets': True})
    stored_personal = await db_session.get(StoredUserSettings, user_id)
    assert stored_personal is not None
    assert 'personal-secret' in jwt_service.decrypt_value(
        stored_personal.encrypted_payload
    )


@pytest.mark.asyncio
async def test_read_through_migrates_only_one_unambiguous_shared_org(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    context = _context('owner', organization_id)
    user_id = await context.get_user_id()
    settings = Settings()
    settings.agent_settings = settings.agent_settings.model_copy(
        update={'mcp_config': {'atlassian': _atlassian_server('legacy-secret')}}
    )
    jwt_service = _jwt_service()
    db_session.add(
        StoredUserSettings(
            user_id=user_id,
            encrypted_payload=jwt_service.encrypt_value(
                settings.model_dump_json(
                    context={'expose_secrets': True, 'persist_settings': True}
                )
            ),
        )
    )
    await db_session.flush()

    service = OrganizationConnectionService(db_session, context, jwt_service)
    migrated = await service.list(organization_id)
    atlassian = next(
        item
        for item in migrated.items
        if item.kind == OrganizationConnectionKind.ATLASSIAN
    )
    assert atlassian.connected is True
    assert atlassian.migrated_from_user is True

    second_org = await _add_shared_membership(
        db_session, 'ambiguous', OrganizationRole.OWNER
    )
    await _add_shared_membership(
        db_session, 'ambiguous', OrganizationRole.OWNER, uuid4()
    )
    ambiguous_context = _context('ambiguous', second_org)
    ambiguous_user_id = await ambiguous_context.get_user_id()
    db_session.add(
        StoredUserSettings(
            user_id=ambiguous_user_id,
            encrypted_payload=jwt_service.encrypt_value(
                settings.model_dump_json(
                    context={'expose_secrets': True, 'persist_settings': True}
                )
            ),
        )
    )
    await db_session.flush()
    ambiguous = await OrganizationConnectionService(
        db_session, ambiguous_context, jwt_service
    ).list(second_org)
    ambiguous_atlassian = next(
        item
        for item in ambiguous.items
        if item.kind == OrganizationConnectionKind.ATLASSIAN
    )
    assert ambiguous_atlassian.connected is False


@pytest.mark.asyncio
async def test_organization_profiles_are_encrypted_and_member_read_only(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    await _add_shared_membership(
        db_session, 'member', OrganizationRole.MEMBER, organization_id
    )
    jwt_service = _jwt_service()
    owner = OrganizationProfileService(
        db_session, _context('owner', organization_id), jwt_service
    )
    await owner.save(
        organization_id,
        'halo',
        OrganizationProfileSaveRequest.model_validate(
            {
                'llm': {
                    'model': 'openai/gpt-4o',
                    'api_key': 'organization-key',
                }
            }
        ),
    )
    await owner.activate(organization_id, 'halo')

    row = await db_session.get(StoredOrganizationProfiles, organization_id)
    assert row is not None
    assert 'organization-key' not in row.encrypted_payload

    member = OrganizationProfileService(
        db_session, _context('member', organization_id), jwt_service
    )
    profiles = await member.load(organization_id)
    assert profiles.active == 'halo'
    assert profiles.require('halo').api_key.get_secret_value() == 'organization-key'
    with pytest.raises(PermissionsError, match='required organization permissions'):
        await member.save(
            organization_id,
            'forbidden',
            OrganizationProfileSaveRequest.model_validate(
                {'llm': {'model': 'openai/gpt-4o'}}
            ),
        )


@pytest.mark.asyncio
async def test_organization_profile_update_rename_activate_delete(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'admin', OrganizationRole.ADMIN
    )
    service = OrganizationProfileService(
        db_session, _context('admin', organization_id), _jwt_service()
    )
    await service.save(
        organization_id,
        'first',
        OrganizationProfileSaveRequest.model_validate(
            {
                'llm': {
                    'model': 'openai/gpt-4o',
                    'api_key': 'first-key',
                }
            }
        ),
    )
    await service.save(
        organization_id,
        'first',
        OrganizationProfileSaveRequest.model_validate(
            {
                'llm': {'model': 'openai/gpt-4.1'},
                'preserve_existing_api_key': True,
            }
        ),
    )
    profiles = await service.rename(organization_id, 'first', 'renamed')
    assert profiles.require('renamed').api_key.get_secret_value() == 'first-key'
    profiles = await service.activate(organization_id, 'renamed')
    assert profiles.active == 'renamed'
    await service.delete(organization_id, 'renamed')
    assert (await service.load(organization_id)).profiles == {}


@pytest.mark.asyncio
async def test_shared_connections_and_active_profile_overlay_conversation_settings():
    organization_id = uuid4()
    personal = Settings()
    personal.agent_settings = personal.agent_settings.model_copy(
        update={'mcp_config': {'perforce': _perforce_server('personal-secret')}}
    )
    user_auth = MagicMock()
    user_auth.get_user_id = AsyncMock(return_value='entra:tenant-1:member')
    user_auth.get_user_settings = AsyncMock(return_value=personal)
    user_auth.get_effective_org_id = AsyncMock(return_value=organization_id)

    session = MagicMock()
    session.commit = AsyncMock()

    @asynccontextmanager
    async def session_context():
        yield session

    config = MagicMock()
    config.db_session.get_async_session_maker = AsyncMock(
        return_value=lambda: session_context()
    )
    config.jwt.get_jwt_service.return_value = _jwt_service()

    connections = MagicMock()
    connections.mcp_servers = AsyncMock(
        return_value={'perforce': _perforce_server('shared-secret')}
    )
    profiles = LLMProfiles()
    profiles.save(
        'halo',
        OrganizationProfileSaveRequest.model_validate(
            {
                'llm': {
                    'model': 'openai/gpt-4.1',
                    'api_key': 'organization-key',
                }
            }
        ).llm,
    )
    profiles.active = 'halo'
    organization_profiles = MagicMock()
    organization_profiles.load = AsyncMock(return_value=profiles)

    with (
        patch(
            'openhands.app_server.config.get_global_config',
            return_value=config,
        ),
        patch(
            'openhands.app_server.user.auth_user_context.OrganizationConnectionService',
            return_value=connections,
        ),
        patch(
            'openhands.app_server.user.auth_user_context.OrganizationProfileService',
            return_value=organization_profiles,
        ),
    ):
        effective = await AuthUserContext(user_auth).get_user_info()

    perforce = effective.agent_settings.mcp_config['perforce']
    assert 'shared-secret' in perforce.model_dump_json(context={'expose_secrets': True})
    assert effective.llm_profiles.active == 'halo'
    assert effective.agent_settings.llm.model == 'openai/gpt-4.1'
