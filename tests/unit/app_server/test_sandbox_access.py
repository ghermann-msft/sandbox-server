from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openhands.app_server.errors import PermissionsError
from openhands.app_server.sandbox.kubernetes_sandbox_service import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    SANDBOX_ID_LABEL,
    SPEC_ID_ANNOTATION,
    KubernetesSandboxService,
)
from openhands.app_server.sandbox.sandbox_access import (
    SandboxAccessService,
    StoredSandboxAccess,
)
from openhands.app_server.sandbox.sandbox_models import (
    SandboxAccessType,
    SandboxAclUpdate,
)
from openhands.app_server.user.trusted_identity import TrustedIdentity

TENANT_A = '11111111-1111-1111-1111-111111111111'
TENANT_B = '22222222-2222-2222-2222-222222222222'
OWNER_ID = '33333333-3333-3333-3333-333333333333'
MEMBER_ID = '44444444-4444-4444-4444-444444444444'
OTHER_ID = '55555555-5555-5555-5555-555555555555'


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine('sqlite+aiosqlite://')
    async with engine.begin() as connection:
        await connection.run_sync(StoredSandboxAccess.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _claim(sandbox_id: str) -> dict:
    return {
        'metadata': {
            'name': f'claim-{sandbox_id}',
            'labels': {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                SANDBOX_ID_LABEL: sandbox_id,
            },
            'annotations': {SPEC_ID_ANNOTATION: 'pool'},
        },
        'spec': {'env': []},
    }


def _service(access_service, custom_objects):
    sandbox_spec_service = AsyncMock()
    sandbox_spec_service.get_default_sandbox_spec.return_value = MagicMock(
        id='pool',
        initial_env={},
        command=None,
        working_dir='/workspace',
    )
    return KubernetesSandboxService(
        sandbox_spec_service=sandbox_spec_service,
        namespace='agents',
        warm_pool='pool',
        claim_name_prefix='claim-',
        sandbox_url_pattern='https://{sandbox_name}.example.com:{port}',
        webhook_base_url='http://app-server',
        exposed_ports=[],
        max_num_sandboxes=5,
        access_service=access_service,
        _custom_objects=custom_objects,
    )


@pytest.mark.asyncio
async def test_owner_member_and_non_member_authorization(session_maker):
    custom_objects = MagicMock()
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [_claim('shared')]
    }

    async with session_maker() as db_session:
        owner_access = SandboxAccessService(
            db_session,
            TrustedIdentity(tenant_id=TENANT_A, object_id=OWNER_ID),
        )
        await owner_access.create_personal_sandbox('shared')
        await owner_access.update_acl(
            'shared',
            SandboxAclUpdate(
                access_type=SandboxAccessType.TEAM,
                member_object_ids=[MEMBER_ID],
            ),
        )
        await db_session.commit()

    async with session_maker() as db_session:
        owner = _service(
            SandboxAccessService(
                db_session,
                TrustedIdentity(tenant_id=TENANT_A, object_id=OWNER_ID),
            ),
            custom_objects,
        )
        assert [item.id for item in (await owner.search_sandboxes()).items] == [
            'shared'
        ]
        acl = await owner.get_sandbox_acl('shared')
        assert acl is not None
        assert acl.is_owner
        assert acl.member_object_ids == [MEMBER_ID]

    async with session_maker() as db_session:
        member = _service(
            SandboxAccessService(
                db_session,
                TrustedIdentity(tenant_id=TENANT_A, object_id=MEMBER_ID),
            ),
            custom_objects,
        )
        assert await member.get_sandbox('shared') is not None
        assert await member.resume_sandbox('shared')
        assert not await member.pause_sandbox('shared')
        assert not await member.delete_sandbox('shared')
        with pytest.raises(PermissionsError, match='Only the sandbox owner'):
            await member.update_sandbox_acl(
                'shared',
                SandboxAclUpdate(
                    access_type=SandboxAccessType.TEAM,
                    member_object_ids=[],
                ),
            )

    async with session_maker() as db_session:
        non_member = _service(
            SandboxAccessService(
                db_session,
                TrustedIdentity(tenant_id=TENANT_A, object_id=OTHER_ID),
            ),
            custom_objects,
        )
        assert (await non_member.search_sandboxes()).items == []
        assert await non_member.get_sandbox('shared') is None
        assert not await non_member.resume_sandbox('shared')

    async with session_maker() as db_session:
        other_tenant = _service(
            SandboxAccessService(
                db_session,
                TrustedIdentity(tenant_id=TENANT_B, object_id=MEMBER_ID),
            ),
            custom_objects,
        )
        assert await other_tenant.get_sandbox('shared') is None


@pytest.mark.asyncio
async def test_new_sandbox_is_private_to_creator(session_maker):
    custom_objects = MagicMock()
    async with session_maker() as db_session:
        service = _service(
            SandboxAccessService(
                db_session,
                TrustedIdentity(tenant_id=TENANT_A, object_id=OWNER_ID),
            ),
            custom_objects,
        )
        info = await service.start_sandbox(sandbox_id='personal')
        await db_session.commit()

        assert info.created_by_user_id == OWNER_ID
        assert info.access_type == SandboxAccessType.PERSONAL
        acl = await service.get_sandbox_acl('personal')
        assert acl is not None
        assert acl.member_object_ids == []


@pytest.mark.asyncio
async def test_owner_identity_comes_from_stored_sandbox_access(session_maker):
    async with session_maker() as db_session:
        access = SandboxAccessService(
            db_session,
            TrustedIdentity(tenant_id=TENANT_A, object_id=OWNER_ID),
        )
        await access.create_personal_sandbox('source')
        await db_session.commit()

    async with session_maker() as db_session:
        access = SandboxAccessService(db_session)
        assert await access.owner_identity('source') == TrustedIdentity(
            tenant_id=TENANT_A,
            object_id=OWNER_ID,
        )
        assert await access.owner_identity('missing') is None


def test_acl_rejects_non_object_id_members():
    with pytest.raises(ValidationError, match='Microsoft Entra object IDs'):
        SandboxAclUpdate(
            access_type=SandboxAccessType.TEAM,
            member_object_ids=['user@example.com'],
        )
