from datetime import timedelta

import jwt
import pytest
from fastapi import Request
from openhands.agent_server.utils import utc_now
from pydantic import SecretStr, ValidationError

from openhands.app_server.errors import AuthError
from openhands.app_server.user.trusted_identity import (
    TrustedIdentityService,
    TrustedIdentityServiceInjector,
)

TENANT_ID = '11111111-1111-1111-1111-111111111111'
OBJECT_ID = '22222222-2222-2222-2222-222222222222'


@pytest.fixture
def service():
    return TrustedIdentityService(
        secret=SecretStr('test-assertion-secret'),
        issuer='halo-bff',
        audience='sandbox-server',
        header_name='X-OpenHands-Identity-Assertion',
        max_lifetime_seconds=300,
        clock_skew_seconds=30,
    )


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/',
            'headers': [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
        }
    )


def _token(**overrides) -> str:
    now = utc_now()
    claims = {
        'iss': 'halo-bff',
        'aud': 'sandbox-server',
        'iat': int(now.timestamp()),
        'exp': int((now + timedelta(minutes=2)).timestamp()),
        'tid': TENANT_ID,
        'oid': OBJECT_ID,
        **overrides,
    }
    return jwt.encode(claims, 'test-assertion-secret', algorithm='HS256')


def test_verifies_signed_tenant_and_object_id(service):
    identity = service.verify_request(
        _request({'X-OpenHands-Identity-Assertion': _token()})
    )

    assert identity.tenant_id == TENANT_ID
    assert identity.object_id == OBJECT_ID


def test_rejects_browser_supplied_raw_identity(service):
    with pytest.raises(AuthError, match='Missing trusted identity assertion'):
        service.verify_request(
            _request(
                {
                    'X-User-Id': 'forged-object',
                    'X-Tenant-Id': 'forged-tenant',
                }
            )
        )


def test_rejects_short_assertion_secret():
    with pytest.raises(ValidationError):
        TrustedIdentityServiceInjector(secret=SecretStr('too-short'))


@pytest.mark.parametrize(
    'token',
    [
        lambda: jwt.encode(
            {
                'iss': 'halo-bff',
                'aud': 'sandbox-server',
                'iat': int(utc_now().timestamp()),
                'exp': int((utc_now() + timedelta(minutes=2)).timestamp()),
                'tid': TENANT_ID,
                'oid': OBJECT_ID,
            },
            'wrong-secret',
            algorithm='HS256',
        ),
        lambda: _token(aud='wrong-audience'),
        lambda: _token(exp=int((utc_now() - timedelta(seconds=1)).timestamp())),
        lambda: _token(
            exp=int((utc_now() + timedelta(minutes=10)).timestamp())
        ),
    ],
)
def test_rejects_invalid_or_overlong_assertions(service, token):
    with pytest.raises(AuthError):
        service.verify_request(
            _request({'X-OpenHands-Identity-Assertion': token()})
        )
