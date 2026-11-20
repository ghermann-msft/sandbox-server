from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from openhands.app_server.hindsight.hindsight_models import (
    HindsightPrincipal,
    HindsightScopeKind,
    HindsightScopeRequest,
)
from openhands.app_server.hindsight.hindsight_service import (
    HindsightBroker,
    HindsightBrokerSettings,
    authorize_explicit_retain_scope,
    resolve_scope,
    retain_requires_approval,
)

PRINCIPAL = HindsightPrincipal(
    tenant_id=UUID('11111111-1111-1111-1111-111111111111'),
    object_id=UUID('22222222-2222-2222-2222-222222222222'),
)


def test_explicit_private_retain_does_not_require_agent_approval():
    scope = resolve_scope(
        HindsightScopeRequest(type=HindsightScopeKind.PRIVATE),
        PRINCIPAL,
        'sandbox-1',
        None,
    )

    authorize_explicit_retain_scope(scope)
    assert retain_requires_approval(scope) is False


def test_explicit_retain_rejects_non_private_scope():
    scope = resolve_scope(
        HindsightScopeRequest(type=HindsightScopeKind.WORKSPACE),
        PRINCIPAL,
        'sandbox-1',
        None,
    )

    with pytest.raises(HTTPException) as exc_info:
        authorize_explicit_retain_scope(scope)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_reflect_uses_agentic_timeout_once_per_scope():
    request_count = 0

    async def reflected(request):
        nonlocal request_count
        request_count += 1
        assert request.extensions['timeout']['read'] == 25
        return httpx.Response(200, json={'text': 'A synthesized answer'})

    settings = HindsightBrokerSettings(
        base_url='https://hindsight.internal',
        api_key='server-secret',
        bank_secret='bank-secret',
    )
    scope = resolve_scope(
        HindsightScopeRequest(type=HindsightScopeKind.PRIVATE),
        PRINCIPAL,
        'sandbox-1',
        None,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reflected),
        timeout=15,
    ) as client:
        broker = HindsightBroker(settings, object(), client)  # type: ignore[arg-type]
        available, text, sources = await broker.reflect(
            'query', [('bank-1', scope)], token_budget=4096
        )

    assert available is True
    assert text == 'A synthesized answer'
    assert sources == []
    assert request_count == 1


@pytest.mark.asyncio
async def test_reflect_timeout_fails_open_without_retry():
    request_count = 0

    async def timed_out(request):
        nonlocal request_count
        request_count += 1
        raise httpx.ReadTimeout('timed out', request=request)

    settings = HindsightBrokerSettings(
        base_url='https://hindsight.internal',
        api_key='server-secret',
        bank_secret='bank-secret',
        fail_open=True,
    )
    scope = resolve_scope(
        HindsightScopeRequest(type=HindsightScopeKind.PRIVATE),
        PRINCIPAL,
        'sandbox-1',
        None,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timed_out),
        timeout=15,
    ) as client:
        broker = HindsightBroker(settings, object(), client)  # type: ignore[arg-type]
        available, text, sources = await broker.reflect(
            'query', [('bank-1', scope)], token_budget=4096
        )

    assert available is False
    assert text is None
    assert sources == []
    assert request_count == 1
