import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversationStartRequest,
)
from openhands.app_server.app_conversation.sql_app_conversation_start_task_service import (
    StoredAppConversationStartTask,
)
from openhands.app_server.errors import PermissionsError
from openhands.app_server.event_callback.webhook_router import (
    MCPOAuthStateWriteBack,
    write_back_mcp_oauth_state,
)
from openhands.app_server.integrations.provider import ProviderToken
from openhands.app_server.organization.agent_profile_models import (
    OrganizationAgentProfileSeeds,
    StoredOrganizationAgentProfiles,
)
from openhands.app_server.organization.agent_profile_router import save_agent_profile
from openhands.app_server.organization.agent_profile_service import (
    OrganizationAgentProfileService,
    get_organization_agent_profile_seeds,
)
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
from openhands.app_server.organization.profile_service import (
    OrganizationProfileService,
    ProfileNotFoundError,
)
from openhands.app_server.sandbox.sandbox_models import SandboxRecord
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.settings.agent_profiles import AgentProfiles
from openhands.app_server.settings.llm_profiles import LLMProfiles
from openhands.app_server.settings.settings_models import Settings
from openhands.app_server.settings.sql_settings_store import StoredUserSettings
from openhands.app_server.user.auth_user_context import AuthUserContext
from openhands.app_server.user.specifiy_user_context import SpecifyUserContext
from openhands.app_server.utils.encryption_key import EncryptionKey
from openhands.app_server.utils.sql_utils import Base, UtcDateTime
from openhands.sdk.mcp.config import MCPOAuthStateResponse, MCPServer
from openhands.sdk.profiles import validate_agent_profile
from openhands.sdk.settings import ACPAgentSettings


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


def _oauth_server(
    access_token: str = 'old-access',
    *,
    url: str = 'https://api.atlassian.com/mcp/',
) -> MCPServer:
    return MCPServer.model_validate(
        {
            'url': url,
            'transport': 'streamable-http',
            'description': 'Atlassian MCP',
            'auth': {
                'strategy': 'oauth2',
                'state': {
                    'tokens': {
                        'access_token': access_token,
                        'refresh_token': 'old-refresh',
                    },
                    'token_expires_at': 100.0,
                },
            },
        }
    )


def _oauth_write_back(
    url: str = 'https://api.atlassian.com/mcp',
) -> MCPOAuthStateWriteBack:
    return MCPOAuthStateWriteBack(
        server_url=url,
        oauth_state=MCPOAuthStateResponse.model_validate(
            {
                'tokens': {
                    'access_token': 'new-access',
                    'refresh_token': 'new-refresh',
                },
                'token_expires_at': 200.0,
            }
        ),
    )


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


@pytest.mark.asyncio
async def test_mcp_oauth_write_back_updates_encrypted_personal_settings(db_session):
    owner_id = 'entra:tenant-1:owner'
    jwt_service = _jwt_service()
    settings = Settings()
    original_server = _oauth_server()
    settings.agent_settings = settings.agent_settings.model_copy(
        update={'mcp_config': {'atlassian': original_server}}
    )
    row = StoredUserSettings(
        user_id=owner_id,
        encrypted_payload=jwt_service.encrypt_value(
            settings.model_dump_json(
                context={'expose_secrets': True, 'persist_settings': True}
            )
        ),
    )
    db_session.add(row)
    await db_session.flush()

    await write_back_mcp_oauth_state(
        _oauth_write_back(),
        SandboxRecord(id='sandbox-1', created_by_user_id=owner_id),
        db_session,
        jwt_service,
    )

    assert 'new-access' not in row.encrypted_payload
    stored = Settings.model_validate_json(
        jwt_service.decrypt_value(row.encrypted_payload)
    )
    server = stored.agent_settings.mcp_config['atlassian']
    assert server.description == original_server.description
    assert server.auth is not None and server.auth.state is not None
    assert server.auth.state.tokens is not None
    assert server.auth.state.tokens.access_token is not None
    assert server.auth.state.tokens.access_token.get_secret_value() == 'new-access'
    assert server.auth.state.tokens.refresh_token is not None
    assert server.auth.state.tokens.refresh_token.get_secret_value() == 'new-refresh'
    assert row.revision == 1


@pytest.mark.asyncio
async def test_mcp_oauth_write_back_updates_encrypted_organization_connection(
    db_session,
):
    owner_id = 'entra:tenant-1:owner'
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    rbac_user = await db_session.scalar(
        select(StoredUser).where(StoredUser.subject_id == 'owner')
    )
    assert rbac_user is not None
    jwt_service = _jwt_service()
    original_server = _oauth_server()
    connection_payload = {
        'server': original_server.model_dump(
            mode='json',
            context={'expose_secrets': True},
            exclude_none=True,
        ),
        'unrelated': {'preserved': True},
    }
    connection = StoredOrganizationConnection(
        organization_id=organization_id,
        kind=OrganizationConnectionKind.ATLASSIAN.value,
        encrypted_payload=jwt_service.encrypt_value(json.dumps(connection_payload)),
        public_metadata={'display_name': 'Atlassian'},
        created_by_user_id=rbac_user.id,
        updated_by_user_id=rbac_user.id,
    )
    db_session.add_all(
        [
            connection,
            StoredAppConversationStartTask(
                id=uuid4(),
                created_by_user_id=owner_id,
                sandbox_id='sandbox-1',
                request=AppConversationStartRequest(organization_id=organization_id),
            ),
        ]
    )
    await db_session.flush()

    await write_back_mcp_oauth_state(
        _oauth_write_back(),
        SandboxRecord(id='sandbox-1', created_by_user_id=owner_id),
        db_session,
        jwt_service,
    )

    assert 'new-access' not in connection.encrypted_payload
    stored_payload = json.loads(jwt_service.decrypt_value(connection.encrypted_payload))
    assert stored_payload['unrelated'] == {'preserved': True}
    stored_server = MCPServer.model_validate(stored_payload['server'])
    assert stored_server.description == original_server.description
    assert stored_server.auth is not None and stored_server.auth.state is not None
    assert stored_server.auth.state.tokens is not None
    assert stored_server.auth.state.tokens.refresh_token is not None
    assert (
        stored_server.auth.state.tokens.refresh_token.get_secret_value()
        == 'new-refresh'
    )
    assert connection.public_metadata == {'display_name': 'Atlassian'}
    assert connection.revision == 1


@pytest.mark.asyncio
async def test_mcp_oauth_write_back_rejects_owner_and_url_mismatch(db_session):
    jwt_service = _jwt_service()
    settings = Settings()
    settings.agent_settings = settings.agent_settings.model_copy(
        update={'mcp_config': {'atlassian': _oauth_server()}}
    )
    db_session.add(
        StoredUserSettings(
            user_id='different-owner',
            encrypted_payload=jwt_service.encrypt_value(
                settings.model_dump_json(
                    context={'expose_secrets': True, 'persist_settings': True}
                )
            ),
        )
    )
    await db_session.flush()

    with pytest.raises(HTTPException) as owner_error:
        await write_back_mcp_oauth_state(
            _oauth_write_back(),
            SandboxRecord(id='sandbox-1', created_by_user_id='owner'),
            db_session,
            jwt_service,
        )
    assert owner_error.value.status_code == 404

    with pytest.raises(HTTPException) as url_error:
        await write_back_mcp_oauth_state(
            _oauth_write_back('https://api.atlassian.com/different'),
            SandboxRecord(id='sandbox-1', created_by_user_id='different-owner'),
            db_session,
            jwt_service,
        )
    assert url_error.value.status_code == 404


@pytest.mark.asyncio
async def test_mcp_oauth_write_back_rejects_ambiguous_durable_owner(db_session):
    owner_id = 'entra:tenant-1:owner'
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    rbac_user = await db_session.scalar(
        select(StoredUser).where(StoredUser.subject_id == 'owner')
    )
    assert rbac_user is not None
    jwt_service = _jwt_service()
    settings = Settings()
    settings.agent_settings = settings.agent_settings.model_copy(
        update={'mcp_config': {'atlassian': _oauth_server()}}
    )
    server_payload = _oauth_server().model_dump(
        mode='json',
        context={'expose_secrets': True},
        exclude_none=True,
    )
    db_session.add_all(
        [
            StoredUserSettings(
                user_id=owner_id,
                encrypted_payload=jwt_service.encrypt_value(
                    settings.model_dump_json(
                        context={
                            'expose_secrets': True,
                            'persist_settings': True,
                        }
                    )
                ),
            ),
            StoredOrganizationConnection(
                organization_id=organization_id,
                kind=OrganizationConnectionKind.ATLASSIAN.value,
                encrypted_payload=jwt_service.encrypt_value(
                    json.dumps({'server': server_payload})
                ),
                public_metadata={},
                created_by_user_id=rbac_user.id,
                updated_by_user_id=rbac_user.id,
            ),
            StoredAppConversationStartTask(
                id=uuid4(),
                created_by_user_id=owner_id,
                sandbox_id='sandbox-1',
                request=AppConversationStartRequest(organization_id=organization_id),
            ),
        ]
    )
    await db_session.flush()

    with pytest.raises(HTTPException) as error:
        await write_back_mcp_oauth_state(
            _oauth_write_back(),
            SandboxRecord(id='sandbox-1', created_by_user_id=owner_id),
            db_session,
            jwt_service,
        )
    assert error.value.status_code == 409


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
    await OrganizationConnectionService(
        db_session, _context('other-owner', other_organization_id), jwt_service
    ).store_github_token(
        other_organization_id,
        ProviderToken(token=SecretStr('other-organization-token')),
    )
    with pytest.raises(PermissionsError, match='Not a member'):
        await owner.list(other_organization_id)
    with pytest.raises(PermissionsError, match='Not a member'):
        await owner.github_token(other_organization_id)


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
async def test_organization_profiles_use_deployment_defaults_without_stored_row(
    db_session, monkeypatch
):
    monkeypatch.setenv('LLM_MODEL', 'azure/gpt-5.6-sol')
    monkeypatch.setenv(
        'LLM_BASE_URL',
        'https://example.openai.azure.com/openai/deployments/gpt-5.6-sol',
    )
    monkeypatch.setenv('LLM_API_VERSION', '2025-04-01-preview')
    monkeypatch.setenv('LLM_AUTH_TYPE', 'api_key')
    organization_id = await _add_shared_membership(
        db_session, 'member', OrganizationRole.MEMBER
    )

    profiles = await OrganizationProfileService(
        db_session, _context('member', organization_id), _jwt_service()
    ).load(organization_id)

    assert profiles.active == 'default'
    default = profiles.require('default')
    assert default.model == 'azure/gpt-5.6-sol'
    assert (
        default.base_url
        == 'https://example.openai.azure.com/openai/deployments/gpt-5.6-sol'
    )
    assert default.api_version == '2025-04-01-preview'
    assert default.auth_type == 'api_key'
    assert await db_session.get(StoredOrganizationProfiles, organization_id) is None


@pytest.mark.asyncio
async def test_organization_profile_mutation_seeds_defaults_before_activation(
    db_session, monkeypatch
):
    monkeypatch.setenv('LLM_MODEL', 'azure/gpt-5.6-sol')
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    service = OrganizationProfileService(
        db_session, _context('owner', organization_id), _jwt_service()
    )

    profiles = await service.save(
        organization_id,
        'custom',
        OrganizationProfileSaveRequest.model_validate(
            {'llm': {'model': 'openai/gpt-4.1'}}
        ),
    )
    assert set(profiles.profiles) == {'default', 'custom'}
    assert profiles.active == 'default'

    profiles = await service.activate(organization_id, 'custom')

    assert profiles.active == 'custom'
    row = await db_session.get(StoredOrganizationProfiles, organization_id)
    assert row is not None
    persisted = await service.load(organization_id)
    assert set(persisted.profiles) == {'default', 'custom'}
    assert persisted.active == 'custom'


@pytest.mark.asyncio
async def test_explicitly_stored_empty_organization_profiles_remain_authoritative(
    db_session, monkeypatch
):
    monkeypatch.setenv('LLM_MODEL', 'azure/gpt-5.6-sol')
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    service = OrganizationProfileService(
        db_session, _context('owner', organization_id), _jwt_service()
    )

    await service.delete(organization_id, 'default')

    assert await db_session.get(StoredOrganizationProfiles, organization_id) is not None
    monkeypatch.setenv('LLM_MODEL', 'azure/gpt-6')
    profiles = await service.load(organization_id)
    assert profiles.profiles == {}
    assert profiles.active is None
    with pytest.raises(ProfileNotFoundError, match="Profile 'default' not found"):
        await service.activate(organization_id, 'default')


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
    agent_profiles = MagicMock()
    agent_profiles.load = AsyncMock(return_value=AgentProfiles())

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
        patch(
            'openhands.app_server.user.auth_user_context.OrganizationAgentProfileService',
            return_value=agent_profiles,
        ),
    ):
        effective = await AuthUserContext(user_auth).get_user_info()

    perforce = effective.agent_settings.mcp_config['perforce']
    assert 'shared-secret' in perforce.model_dump_json(context={'expose_secrets': True})
    assert effective.llm_profiles.active == 'halo'
    assert effective.agent_settings.llm.model == 'openai/gpt-4.1'


@pytest.mark.asyncio
async def test_agent_profiles_are_scoped_durable_and_member_read_only(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    await _add_shared_membership(
        db_session, 'member', OrganizationRole.MEMBER, organization_id
    )
    owner = OrganizationAgentProfileService(
        db_session, _context('owner', organization_id)
    )
    profile = validate_agent_profile(
        {
            'name': 'copilot-ui',
            'agent_kind': 'acp',
            'acp_server': 'claude-code',
        }
    )

    saved = await owner.save(organization_id, profile)
    profile_id = next(iter(saved.profiles))
    await owner.activate(organization_id, profile_id)
    await db_session.commit()

    row = await db_session.get(StoredOrganizationAgentProfiles, organization_id)
    assert row is not None
    assert 'copilot-ui' in row.payload
    assert 'api_key' not in row.payload

    member = OrganizationAgentProfileService(
        db_session, _context('member', organization_id)
    )
    loaded = await member.load(organization_id)
    assert loaded.active == profile_id
    assert loaded.profiles[profile_id].agent_kind == 'acp'
    assert loaded.profiles[profile_id].acp_server == 'claude-code'

    with pytest.raises(PermissionsError):
        await member.save(organization_id, profile)

    renamed = await owner.rename(organization_id, 'copilot-ui', 'copilot-renamed')
    assert renamed.active == profile_id
    assert renamed.profiles[profile_id].name == 'copilot-renamed'
    deleted = await owner.delete(organization_id, 'copilot-renamed')
    assert deleted.active is None
    assert deleted.profiles == {}


def _agent_profile_seeds() -> OrganizationAgentProfileSeeds:
    return OrganizationAgentProfileSeeds.model_validate(
        {
            'profiles': {
                'codex': {
                    'agent_kind': 'acp',
                    'acp_server': 'codex',
                    'acp_prompt_timeout': 1800,
                    'acp_startup_timeout': 90,
                },
                'default': {
                    'agent_kind': 'openhands',
                    'llm_profile_ref': 'default',
                },
            },
            'active_profile': 'codex',
        }
    )


@pytest.mark.asyncio
async def test_agent_profile_seeds_initialize_missing_durable_state(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    service = OrganizationAgentProfileService(
        db_session,
        _context('owner', organization_id),
        seeds=_agent_profile_seeds(),
    )

    profiles = await service.load(organization_id)

    expected_codex_id = str(uuid5(organization_id, 'codex'))
    assert {profile.name for profile in profiles.profiles.values()} == {
        'codex',
        'default',
    }
    assert profiles.active == expected_codex_id
    assert profiles.profiles[expected_codex_id].acp_server == 'codex'
    row = await db_session.get(StoredOrganizationAgentProfiles, organization_id)
    assert row is not None
    assert AgentProfiles.model_validate_json(row.payload) == profiles


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_profiles', [AgentProfiles(), None])
async def test_agent_profile_seeds_preserve_existing_durable_state(
    db_session,
    existing_profiles,
):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    if existing_profiles is None:
        existing_profiles = AgentProfiles()
        custom = validate_agent_profile(
            {
                'name': 'custom',
                'agent_kind': 'acp',
                'acp_server': 'codex',
            }
        )
        existing_profiles.save(custom)
    user = await db_session.scalar(
        select(StoredUser).where(StoredUser.subject_id == 'owner')
    )
    assert user is not None
    db_session.add(
        StoredOrganizationAgentProfiles(
            organization_id=organization_id,
            payload=existing_profiles.model_dump_json(),
            created_by_user_id=user.id,
            updated_by_user_id=user.id,
        )
    )
    await db_session.flush()
    service = OrganizationAgentProfileService(
        db_session,
        _context('owner', organization_id),
        seeds=_agent_profile_seeds(),
    )

    loaded = await service.load(organization_id)

    assert loaded == existing_profiles


def test_invalid_agent_profile_seed_configuration_surfaces(monkeypatch):
    monkeypatch.setenv(
        'OH_ORGANIZATION_AGENT_PROFILE_SEEDS_JSON',
        '{"profiles":{"codex":{"agent_kind":"acp","acp_server":"invalid"}},"active_profile":"codex"}',
    )
    get_organization_agent_profile_seeds.cache_clear()

    with pytest.raises(ValidationError):
        get_organization_agent_profile_seeds()

    get_organization_agent_profile_seeds.cache_clear()


def test_agent_profile_seed_configuration_is_parsed_once(monkeypatch):
    monkeypatch.setenv(
        'OH_ORGANIZATION_AGENT_PROFILE_SEEDS_JSON',
        '{"profiles":{"codex":{"agent_kind":"acp","acp_server":"codex"}},"active_profile":"codex"}',
    )
    get_organization_agent_profile_seeds.cache_clear()
    parsed = get_organization_agent_profile_seeds()
    monkeypatch.setenv('OH_ORGANIZATION_AGENT_PROFILE_SEEDS_JSON', 'invalid')

    assert get_organization_agent_profile_seeds() is parsed

    get_organization_agent_profile_seeds.cache_clear()


@pytest.mark.asyncio
async def test_first_agent_profile_api_save_creates_durable_row(db_session):
    organization_id = await _add_shared_membership(
        db_session, 'owner', OrganizationRole.OWNER
    )
    response = await save_agent_profile(
        name='codex',
        body={
            'agent_kind': 'acp',
            'acp_server': 'codex',
            'acp_prompt_timeout': 1800,
            'acp_startup_timeout': 90,
        },
        db_session=db_session,
        user_context=_context('owner', organization_id),
    )

    row = await db_session.get(StoredOrganizationAgentProfiles, organization_id)
    assert row is not None
    assert response.name == 'codex'
    assert AgentProfiles.model_validate_json(row.payload).load('codex').acp_server == (
        'codex'
    )


def test_agent_profile_timestamps_use_utc_server_defaults():
    for column_name in ('created_at', 'updated_at'):
        column = StoredOrganizationAgentProfiles.__table__.c[column_name]
        assert isinstance(column.type, UtcDateTime)
        assert column.server_default is not None


def test_organization_connection_timestamps_use_utc_server_defaults():
    for column_name in ('created_at', 'updated_at'):
        column = StoredOrganizationConnection.__table__.c[column_name]
        assert isinstance(column.type, UtcDateTime)
        assert column.server_default is not None


@pytest.mark.asyncio
async def test_explicit_agent_profile_override_resolves_acp_and_provenance():
    organization_id = uuid4()
    profile = validate_agent_profile(
        {
            'name': 'copilot-ui',
            'agent_kind': 'acp',
            'acp_server': 'claude-code',
        }
    )
    profiles = AgentProfiles()
    profiles.save(profile)
    profile_id = str(profile.id)

    user_auth = MagicMock()
    user_auth.get_user_id = AsyncMock(return_value='entra:tenant-1:member')
    user_auth.get_user_settings = AsyncMock(return_value=Settings())
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
    connections.mcp_servers = AsyncMock(return_value={})
    organization_profiles = MagicMock()
    organization_profiles.load = AsyncMock(return_value=LLMProfiles())
    agent_profiles = MagicMock()
    agent_profiles.load = AsyncMock(return_value=profiles)

    with (
        patch('openhands.app_server.config.get_global_config', return_value=config),
        patch(
            'openhands.app_server.user.auth_user_context.OrganizationConnectionService',
            return_value=connections,
        ),
        patch(
            'openhands.app_server.user.auth_user_context.OrganizationProfileService',
            return_value=organization_profiles,
        ),
        patch(
            'openhands.app_server.user.auth_user_context.OrganizationAgentProfileService',
            return_value=agent_profiles,
        ),
    ):
        effective = await AuthUserContext(user_auth).get_user_info(
            resolve_agent_profile=True,
            override_agent_profile_id=profile_id,
        )

    assert isinstance(effective.agent_settings, ACPAgentSettings)
    assert effective.agent_settings.acp_server == 'claude-code'
    assert effective.active_agent_profile_id == profile_id
    assert effective.active_agent_profile_revision == profile.revision
