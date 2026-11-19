import time
from unittest.mock import patch

import jwt
import pytest
from fastapi import Request

from openhands.app_server.errors import AuthError
from openhands.app_server.user_auth.trusted_identity_user_auth import (
    TrustedIdentityUserAuth,
    decode_identity_assertion,
)

_ENV = {
    'OH_TRUSTED_IDENTITY_ASSERTION_SECRET': 'test-shared-secret',
    'OH_TRUSTED_IDENTITY_ASSERTION_ISSUER': 'openhands-halo-bff',
    'OH_TRUSTED_IDENTITY_ASSERTION_AUDIENCE': 'openhands-sandbox-server',
    'OH_TRUSTED_IDENTITY_ASSERTION_MAX_LIFETIME_SECONDS': '300',
    'OH_TRUSTED_IDENTITY_ASSERTION_CLOCK_SKEW_SECONDS': '30',
}


def _token(**overrides) -> str:
    now = int(time.time())
    claims = {
        'oid': 'object-id',
        'tid': 'tenant-id',
        'sub': 'object-id',
        'iss': 'openhands-halo-bff',
        'aud': 'openhands-sandbox-server',
        'iat': now,
        'exp': now + 60,
        'preferred_username': 'user@example.com',
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        _ENV['OH_TRUSTED_IDENTITY_ASSERTION_SECRET'],
        algorithm='HS256',
        headers={'typ': 'JWT'},
    )


def _request(token: str | None) -> Request:
    headers = []
    if token:
        headers.append((b'x-openhands-identity-assertion', token.encode()))
    return Request({'type': 'http', 'headers': headers})


@pytest.mark.asyncio
async def test_resolves_stable_tenant_and_object_user_id():
    with patch.dict('os.environ', _ENV, clear=False):
        auth = await TrustedIdentityUserAuth.get_instance(_request(_token()))

    assert await auth.get_user_id() == 'entra:tenant-id:object-id'
    assert await auth.get_user_email() == 'user@example.com'
    assert (await auth.get_authentication_identity()).model_dump() == {
        'provider': 'entra',
        'subject_id': 'object-id',
        'tenant_id': 'tenant-id',
    }


@pytest.mark.asyncio
async def test_missing_assertion_fails_closed():
    with patch.dict('os.environ', _ENV, clear=False):
        with pytest.raises(AuthError, match='assertion required'):
            await TrustedIdentityUserAuth.get_instance(_request(None))


@pytest.mark.parametrize(
    'token',
    [
        lambda: _token(sub='different-object'),
        lambda: _token(tid=''),
        lambda: _token(exp=int(time.time()) + 301),
        lambda: jwt.encode(
            {
                'oid': 'object-id',
                'tid': 'tenant-id',
                'sub': 'object-id',
                'iss': 'openhands-halo-bff',
                'aud': 'openhands-sandbox-server',
                'iat': int(time.time()),
                'exp': int(time.time()) + 60,
            },
            'wrong-secret',
            algorithm='HS256',
            headers={'typ': 'JWT'},
        ),
    ],
)
def test_rejects_invalid_assertions(token):
    with patch.dict('os.environ', _ENV, clear=False):
        with pytest.raises(AuthError):
            decode_identity_assertion(token())


def test_rejects_wrong_protected_header_type():
    now = int(time.time())
    token = jwt.encode(
        {
            'oid': 'object-id',
            'tid': 'tenant-id',
            'sub': 'object-id',
            'iss': 'openhands-halo-bff',
            'aud': 'openhands-sandbox-server',
            'iat': now,
            'exp': now + 60,
        },
        _ENV['OH_TRUSTED_IDENTITY_ASSERTION_SECRET'],
        algorithm='HS256',
        headers={'typ': 'not-jwt'},
    )
    with patch.dict('os.environ', _ENV, clear=False):
        with pytest.raises(AuthError, match='header'):
            decode_identity_assertion(token)


@pytest.mark.asyncio
async def test_get_for_user_rejects_incomplete_stable_id():
    with pytest.raises(AuthError, match='user id'):
        await TrustedIdentityUserAuth.get_for_user('entra:tenant-id:')
