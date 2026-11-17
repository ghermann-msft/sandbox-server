import json
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from openhands.agent_server.utils import utc_now
from openhands.app_server.hindsight.hindsight_config import (
    get_hindsight_deployment_id,
    get_hindsight_scope_bindings_json_file,
    load_hindsight_scope_bindings,
)
from openhands.app_server.hindsight.hindsight_models import (
    HindsightAuthorizationContext,
    HindsightOrganizationBinding,
    HindsightPrincipal,
    HindsightReadRequest,
    HindsightRepositoryScopeBinding,
    HindsightScopeBindings,
    HindsightScopeKind,
    HindsightScopeRequest,
)
from openhands.app_server.hindsight.hindsight_service import (
    HindsightBroker,
    HindsightBrokerSettings,
    HindsightOutboxStatus,
    StoredHindsightOutbox,
    authorize_conversation_organization,
    authorize_scope_operation,
    content_sha256,
    derive_bank_id,
    derive_event_id,
    resolve_scope,
    retain_requires_approval,
    validate_retain_approval,
)
from openhands.app_server.services.db_session_injector import DbSessionInjector
from openhands.app_server.utils.sql_utils import Base

PRINCIPAL = HindsightPrincipal(
    tenant_id=UUID('11111111-1111-1111-1111-111111111111'),
    object_id=UUID('22222222-2222-2222-2222-222222222222'),
)


@pytest.fixture
async def session_maker(tmp_path):
    injector = DbSessionInjector(persistence_dir=tmp_path)
    engine = await injector.get_async_db_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield await injector.get_async_session_maker()
    await engine.dispose()


def _settings(**overrides):
    values = {
        'base_url': 'https://hindsight.internal',
        'api_key': 'server-secret',
        'bank_secret': 'bank-secret',
    }
    values.update(overrides)
    return HindsightBrokerSettings(**values)


def test_file_backed_secret_and_scope_contract(tmp_path, monkeypatch):
    api_key_file = tmp_path / 'hindsightApiKey'
    bank_secret_file = tmp_path / 'hindsightBankSecret'
    bindings_file = tmp_path / 'hindsightScopeBindingsJson'
    api_key_file.write_text('api-secret\n', encoding='utf-8')
    bank_secret_file.write_text('bank-secret\n', encoding='utf-8')
    bindings_file.write_text(
        json.dumps(
            {
                'organizations': {
                    'personal-1': {
                        'display_name': 'Personal',
                        'kind': 'personal',
                    },
                    'halo-studios': {
                        'display_name': 'Halo Studios',
                        'kind': 'shared',
                    },
                },
                'repositories': {
                    'github:openhands/sandbox-server': {
                        'organization_ids': ['personal-1', 'halo-studios'],
                    }
                },
            }
        ),
        encoding='utf-8',
    )
    monkeypatch.setenv('OH_HINDSIGHT_SCOPE_BINDINGS_JSON_FILE', str(bindings_file))
    monkeypatch.setenv('OH_HINDSIGHT_DEPLOYMENT_ID', 'halo-ppe')

    settings = HindsightBrokerSettings(
        base_url='https://hindsight.internal',
        api_key_file=api_key_file,
        bank_secret_file=bank_secret_file,
    )
    bindings = load_hindsight_scope_bindings(bindings_file)

    assert settings.get_api_key() == 'api-secret'
    assert settings.get_bank_secret() == 'bank-secret'
    assert bindings.organizations['personal-1'].kind == 'personal'
    assert bindings.organizations['halo-studios'].kind == 'shared'
    assert get_hindsight_scope_bindings_json_file() == bindings_file
    assert get_hindsight_deployment_id() == 'halo-ppe'


def test_sdk_scope_list_shape_and_private_alias_are_canonicalized():
    request = HindsightReadRequest.model_validate(
        {
            'conversation_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            'query': 'memory',
            'scope': [
                {'type': 'private_user', 'id': PRINCIPAL.authority_id},
                {'type': 'server', 'id': 'halo-ppe'},
            ],
        }
    )

    assert request.scope[0].type == HindsightScopeKind.PRIVATE
    assert request.scope[1].type == HindsightScopeKind.SERVER


def test_repository_scope_must_match_conversation_repository():
    scope = HindsightScopeRequest(
        kind=HindsightScopeKind.REPOSITORY,
        repository='github.com/other/repo',
    )

    with pytest.raises(HTTPException) as exc_info:
        resolve_scope(scope, PRINCIPAL, 'sandbox-1', 'OpenHands/sandbox-server')

    assert exc_info.value.status_code == 403


def test_repository_scope_includes_git_provider():
    request = HindsightScopeRequest(
        kind=HindsightScopeKind.REPOSITORY,
        repository='OpenHands/sandbox-server',
    )

    github_scope = resolve_scope(
        request,
        PRINCIPAL,
        'sandbox-1',
        'OpenHands/sandbox-server',
        'github',
    )
    gitlab_scope = resolve_scope(
        request,
        PRINCIPAL,
        'sandbox-1',
        'OpenHands/sandbox-server',
        'gitlab',
    )

    assert github_scope.id != gitlab_scope.id


def test_authority_and_scope_are_isolated_in_bank_identity():
    private_scope = resolve_scope(
        HindsightScopeRequest(kind='private_user'),
        PRINCIPAL,
        'sandbox-1',
        None,
    )
    workspace_scope = resolve_scope(
        HindsightScopeRequest(kind=HindsightScopeKind.WORKSPACE),
        PRINCIPAL,
        'sandbox-1',
        None,
    )
    other_principal = PRINCIPAL.model_copy(
        update={'object_id': UUID('33333333-3333-3333-3333-333333333333')}
    )

    assert derive_bank_id(PRINCIPAL, private_scope, 'secret') != derive_bank_id(
        PRINCIPAL, workspace_scope, 'secret'
    )
    assert derive_bank_id(PRINCIPAL, private_scope, 'secret') != derive_bank_id(
        other_principal, private_scope, 'secret'
    )
    assert derive_bank_id(PRINCIPAL, workspace_scope, 'secret') == derive_bank_id(
        other_principal, workspace_scope, 'secret'
    )


def test_server_scope_is_shared_across_users_and_rejects_forged_id():
    request = HindsightScopeRequest(type=HindsightScopeKind.SERVER, id='halo-ppe')
    first = resolve_scope(
        request,
        PRINCIPAL,
        'sandbox-1',
        None,
        deployment_id='halo-ppe',
    )
    other_principal = HindsightPrincipal(
        tenant_id=UUID('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'),
        object_id=UUID('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'),
    )
    second = resolve_scope(
        request,
        other_principal,
        'sandbox-2',
        None,
        deployment_id='halo-ppe',
    )

    assert derive_bank_id(PRINCIPAL, first, 'secret') == derive_bank_id(
        other_principal, second, 'secret'
    )
    assert retain_requires_approval(first) is False
    with pytest.raises(HTTPException) as exc_info:
        resolve_scope(
            HindsightScopeRequest(type=HindsightScopeKind.SERVER, id='forged'),
            PRINCIPAL,
            'sandbox-1',
            None,
            deployment_id='halo-ppe',
        )
    assert exc_info.value.status_code == 403


def test_organization_requires_openhands_rbac_permission_and_selection():
    repository_id = 'github:openhands/sandbox-server'
    bindings = HindsightScopeBindings(
        organizations={
            'personal-1': HindsightOrganizationBinding(
                display_name='Personal',
                kind='personal',
            ),
            'halo-studios': HindsightOrganizationBinding(
                display_name='Halo Studios',
                kind='shared',
            ),
        },
        repositories={
            repository_id: HindsightRepositoryScopeBinding(
                organization_ids=['personal-1', 'halo-studios'],
            )
        },
    )

    personal = resolve_scope(
        HindsightScopeRequest(type=HindsightScopeKind.ORGANIZATION, id='personal-1'),
        PRINCIPAL,
        'sandbox-1',
        'OpenHands/sandbox-server',
        'github',
        scope_bindings=bindings,
    )
    shared = resolve_scope(
        HindsightScopeRequest(type=HindsightScopeKind.ORGANIZATION, id='halo-studios'),
        PRINCIPAL,
        'sandbox-1',
        'OpenHands/sandbox-server',
        'github',
        scope_bindings=bindings,
    )

    assert personal.id == 'personal-1'
    assert shared.id == 'halo-studios'
    assert derive_bank_id(PRINCIPAL, personal, 'secret') != derive_bank_id(
        PRINCIPAL, shared, 'secret'
    )
    assert retain_requires_approval(personal) is False
    assert retain_requires_approval(shared) is False

    personal_authorization = HindsightAuthorizationContext(
        tenant_id=PRINCIPAL.tenant_id,
        object_id=PRINCIPAL.object_id,
        organization_id='personal-1',
        role='owner',
        permissions={'hindsight:recall', 'hindsight:retain'},
    )
    authorize_scope_operation(personal_authorization, personal, 'hindsight:recall')
    authorize_scope_operation(personal_authorization, personal, 'hindsight:retain')

    with pytest.raises(HTTPException) as exc_info:
        authorize_scope_operation(personal_authorization, shared, 'hindsight:recall')
    assert exc_info.value.status_code == 403

    read_only_shared = HindsightAuthorizationContext(
        tenant_id=PRINCIPAL.tenant_id,
        object_id=PRINCIPAL.object_id,
        organization_id='halo-studios',
        role='member',
        permissions={'hindsight:recall'},
    )
    authorize_scope_operation(read_only_shared, shared, 'hindsight:recall')
    authorize_conversation_organization('halo-studios', read_only_shared, [shared])
    with pytest.raises(HTTPException) as exc_info:
        authorize_scope_operation(read_only_shared, shared, 'hindsight:retain')
    assert exc_info.value.status_code == 403

    with pytest.raises(HTTPException) as exc_info:
        authorize_conversation_organization('personal-1', read_only_shared, [shared])
    assert exc_info.value.status_code == 403


def test_event_id_is_deterministic_and_bound_to_content():
    scope = resolve_scope(
        HindsightScopeRequest(kind=HindsightScopeKind.WORKSPACE),
        PRINCIPAL,
        'sandbox-1',
        None,
    )
    first = derive_event_id(
        PRINCIPAL, 'conversation-1', 'event-1', scope, content_sha256('memory')
    )
    second = derive_event_id(
        PRINCIPAL, 'conversation-1', 'event-1', scope, content_sha256('memory')
    )
    changed = derive_event_id(
        PRINCIPAL, 'conversation-1', 'event-1', scope, content_sha256('other')
    )

    assert first == second
    assert first != changed


def test_sandbox_cannot_reuse_approval_for_different_content():
    scope = resolve_scope(
        HindsightScopeRequest(kind=HindsightScopeKind.WORKSPACE),
        PRINCIPAL,
        'sandbox-1',
        None,
    )
    approval = {
        'purpose': 'hindsight_retain_approval',
        'user_id': 'user-1',
        'tenant_id': str(PRINCIPAL.tenant_id),
        'object_id': str(PRINCIPAL.object_id),
        'conversation_id': 'conversation-1',
        'sandbox_id': 'sandbox-1',
        'source_event_id': 'event-1',
        'scope': scope.model_dump(mode='json'),
        'content_sha256': content_sha256('approved'),
    }

    with pytest.raises(HTTPException) as exc_info:
        validate_retain_approval(
            approval,
            user_id='user-1',
            principal=PRINCIPAL,
            conversation_id='conversation-1',
            sandbox_id='sandbox-1',
            source_event_id='event-1',
            scope=scope,
            content_digest=content_sha256('substituted'),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_enqueue_is_idempotent(session_maker):
    broker = HindsightBroker(_settings(), session_maker)
    payload = {'content': 'remember this'}

    first, first_duplicate = await broker.enqueue('event-1', 'bank-1', payload)
    second, second_duplicate = await broker.enqueue('event-1', 'bank-1', payload)

    assert first.event_id == second.event_id
    assert first_duplicate is False
    assert second_duplicate is True
    async with session_maker() as session:
        assert await session.get(StoredHindsightOutbox, 'event-1') is not None


@pytest.mark.asyncio
async def test_recall_outage_fails_open_without_memory(session_maker):
    async def unavailable(_request):
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        broker = HindsightBroker(_settings(fail_open=True), session_maker, client)
        scope = resolve_scope(
            HindsightScopeRequest(kind=HindsightScopeKind.WORKSPACE),
            PRINCIPAL,
            'sandbox-1',
            None,
        )
        available, memories = await broker.recall(
            'query', [('bank-1', scope)], max_results=10, token_budget=1024
        )

    assert available is False
    assert memories == []


@pytest.mark.asyncio
async def test_recall_outage_can_fail_closed(session_maker):
    async def unavailable(_request):
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        broker = HindsightBroker(_settings(fail_open=False), session_maker, client)
        scope = resolve_scope(
            HindsightScopeRequest(kind=HindsightScopeKind.WORKSPACE),
            PRINCIPAL,
            'sandbox-1',
            None,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await broker.recall(
                'query', [('bank-1', scope)], max_results=10, token_budget=1024
            )


@pytest.mark.asyncio
async def test_recall_preserves_required_provenance(session_maker):
    async def recalled(_request):
        return httpx.Response(
            200,
            json={
                'results': [
                    {
                        'id': 'memory-1',
                        'text': 'A remembered fact',
                        'occurred_start': '2026-01-02T03:04:05Z',
                        'metadata': {
                            'source_event_id': 'event-1',
                            'source_conversation_id': 'conversation-1',
                        },
                        'scores': {'final': 0.75},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(recalled)) as client:
        broker = HindsightBroker(_settings(), session_maker, client)
        scope = resolve_scope(
            HindsightScopeRequest(kind=HindsightScopeKind.WORKSPACE),
            PRINCIPAL,
            'sandbox-1',
            None,
        )
        available, memories = await broker.recall(
            'query', [('bank-1', scope)], max_results=10, token_budget=1024
        )

    assert available is True
    assert memories[0].source_memory_id == 'memory-1'
    assert memories[0].source_event_id == 'event-1'
    assert memories[0].source_conversation_id == 'conversation-1'
    assert memories[0].date is not None
    assert memories[0].scope == scope
    assert memories[0].uncertainty == 0.25
    assert memories[0].untrusted is True


@pytest.mark.asyncio
async def test_reflect_preserves_evidence_provenance(session_maker):
    async def reflected(request):
        assert request.read().decode()
        return httpx.Response(
            200,
            json={
                'text': 'A synthesized answer',
                'based_on': {
                    'memories': [
                        {
                            'id': 'memory-1',
                            'text': 'Evidence',
                            'occurred_start': '2026-01-02T03:04:05Z',
                            'metadata': {
                                'source_event_id': 'event-1',
                                'source_conversation_id': 'conversation-1',
                            },
                        }
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reflected)) as client:
        broker = HindsightBroker(_settings(), session_maker, client)
        scope = resolve_scope(
            HindsightScopeRequest(kind=HindsightScopeKind.WORKSPACE),
            PRINCIPAL,
            'sandbox-1',
            None,
        )
        available, text, sources = await broker.reflect(
            'query', [('bank-1', scope)], token_budget=4096
        )

    assert available is True
    assert text == 'A synthesized answer'
    assert sources[0].source_memory_id == 'memory-1'
    assert sources[0].source_event_id == 'event-1'
    assert sources[0].scope == scope
    assert sources[0].untrusted is True


@pytest.mark.asyncio
async def test_delivery_retries_then_dead_letters(session_maker):
    async def unavailable(_request):
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        broker = HindsightBroker(
            _settings(max_attempts=2), session_maker, http_client=client
        )
        await broker.enqueue('event-1', 'bank-1', {'content': 'memory'})
        assert await broker.deliver_once() is True

        async with session_maker() as session:
            row = await session.get(StoredHindsightOutbox, 'event-1')
            assert row is not None
            assert row.status == HindsightOutboxStatus.RETRY
            row.next_attempt_at = utc_now() - timedelta(seconds=1)
            await session.commit()

        assert await broker.deliver_once() is True

    async with session_maker() as session:
        row = await session.get(StoredHindsightOutbox, 'event-1')
        assert row is not None
        assert row.status == HindsightOutboxStatus.DEAD_LETTER
        assert row.attempt_count == 2
