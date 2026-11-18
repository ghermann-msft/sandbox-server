from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxOperatingSystem,
    SandboxSpecAvailability,
    SandboxSpecCreate,
    SandboxSpecInfo,
    SandboxSpecUpdate,
)
from openhands.app_server.sandbox.sql_sandbox_spec_service import (
    SQLSandboxSpecService,
    StoredSandboxSpec,
)


@pytest.fixture
async def service():
    engine = create_async_engine(
        'sqlite+aiosqlite:///:memory:',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(StoredSandboxSpec.__table__.create)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        yield SQLSandboxSpecService(session)
    await engine.dispose()


async def test_bootstrap_seed_is_idempotent_and_updates_bootstrap_rows(service):
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    initial = SandboxSpecInfo(
        id='windows-server-2025-amd64',
        command=None,
        source='bootstrap',
        immutable=True,
        bootstrap_revision='v1',
        image='example/image@sha256:' + '1' * 64,
        operating_system=SandboxOperatingSystem.WINDOWS,
        platform='windows/amd64',
        created_at=created_at,
    )
    await service.seed_sandbox_specs([initial])
    updated = initial.model_copy(
        update={
            'bootstrap_revision': 'v2',
            'image': 'example/image@sha256:' + '2' * 64,
        }
    )

    await service.seed_sandbox_specs([updated])

    stored = await service.get_sandbox_spec(initial.id, include_unavailable=True)
    assert stored is not None
    assert stored.bootstrap_revision == 'v2'
    assert stored.image == updated.image
    assert stored.created_at == created_at


async def test_bootstrap_seed_does_not_rewrite_unchanged_profile(service):
    profile = SandboxSpecInfo(
        id='linux-standard',
        command=None,
        source='bootstrap',
        immutable=True,
    )
    await service.seed_sandbox_specs([profile])
    stored = await service.get_sandbox_spec(profile.id, include_unavailable=True)
    assert stored is not None

    await service.seed_sandbox_specs([profile])

    unchanged = await service.get_sandbox_spec(profile.id, include_unavailable=True)
    assert unchanged is not None
    assert unchanged.updated_at == stored.updated_at


async def test_public_discovery_excludes_unavailable_and_disabled_profiles(service):
    await service.seed_sandbox_specs(
        [
            SandboxSpecInfo(id='available', command=None, source='bootstrap'),
            SandboxSpecInfo(
                id='unavailable',
                command=None,
                source='bootstrap',
                availability=SandboxSpecAvailability.UNAVAILABLE,
            ),
        ]
    )

    page = await service.search_sandbox_specs()

    assert [profile.id for profile in page.items] == ['available']
    assert (
        await service.get_sandbox_spec('unavailable', include_unavailable=True)
        is not None
    )


async def test_admin_profile_crud_and_soft_delete(service):
    created = await service.create_sandbox_spec(
        SandboxSpecCreate(profile=SandboxSpecInfo(id='admin-build', command=None))
    )
    assert created.source == 'admin'
    assert created.immutable is False

    updated = await service.update_sandbox_spec(
        created.id,
        SandboxSpecUpdate(
            display_name='Build',
            max_concurrent_per_user=2,
            startup_timeout_seconds=240,
            operating_system=SandboxOperatingSystem.LINUX,
        ),
    )
    assert updated is not None
    assert updated.max_concurrent_per_user == 2
    assert updated.startup_timeout_seconds == 240
    assert updated.operating_system == SandboxOperatingSystem.LINUX

    assert await service.delete_sandbox_spec(created.id) is True
    assert await service.get_sandbox_spec(created.id) is None
    retired = await service.get_sandbox_spec(created.id, include_unavailable=True)
    assert retired is not None
    assert retired.availability == SandboxSpecAvailability.DISABLED


async def test_admin_update_revalidates_windows_image_contract(service):
    profile = SandboxSpecInfo(
        id='windows-build',
        command=None,
        operating_system=SandboxOperatingSystem.WINDOWS,
        platform='windows/amd64',
        image='example/image@sha256:' + '1' * 64,
    )
    await service.create_sandbox_spec(SandboxSpecCreate(profile=profile))

    with pytest.raises(ValueError, match='digest-pinned'):
        await service.update_sandbox_spec(
            profile.id,
            SandboxSpecUpdate(image='example/image:mutable'),
        )


async def test_bootstrap_profile_rejects_admin_mutation(service):
    profile = SandboxSpecInfo(
        id='immutable',
        command=None,
        source='bootstrap',
        immutable=True,
    )
    await service.seed_sandbox_specs([profile])

    with pytest.raises(ValueError, match='immutable'):
        await service.update_sandbox_spec(
            profile.id, SandboxSpecUpdate(display_name='changed')
        )
    with pytest.raises(ValueError, match='immutable'):
        await service.delete_sandbox_spec(profile.id)
