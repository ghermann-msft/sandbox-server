from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Request

from openhands.app_server.errors import AuthError
from openhands.app_server.user_auth.default_user_auth import DefaultUserAuth
from openhands.app_server.user_auth.user_auth import UserAuth

_DEFAULT_HEADER = 'X-OpenHands-Identity-Assertion'
_DEFAULT_MAX_LIFETIME_SECONDS = 300
_DEFAULT_CLOCK_SKEW_SECONDS = 30
_REQUIRED_CLAIMS = ('oid', 'tid', 'sub', 'iss', 'aud', 'iat', 'exp')


def _required_env(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise AuthError('Trusted identity is not configured')
    return value


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise AuthError(f'Invalid trusted identity configuration: {name}') from exc
    if value < 0:
        raise AuthError(f'Invalid trusted identity configuration: {name}')
    return value


def decode_identity_assertion(assertion: str) -> dict[str, Any]:
    secret = _required_env('OH_TRUSTED_IDENTITY_ASSERTION_SECRET')
    issuer = _required_env('OH_TRUSTED_IDENTITY_ASSERTION_ISSUER')
    audience = _required_env('OH_TRUSTED_IDENTITY_ASSERTION_AUDIENCE')
    leeway = _positive_int_env(
        'OH_TRUSTED_IDENTITY_ASSERTION_CLOCK_SKEW_SECONDS',
        _DEFAULT_CLOCK_SKEW_SECONDS,
    )
    max_lifetime = _positive_int_env(
        'OH_TRUSTED_IDENTITY_ASSERTION_MAX_LIFETIME_SECONDS',
        _DEFAULT_MAX_LIFETIME_SECONDS,
    )

    try:
        header = jwt.get_unverified_header(assertion)
        if header.get('alg') != 'HS256' or header.get('typ') != 'JWT':
            raise AuthError('Invalid trusted identity assertion header')
        claims = jwt.decode(
            assertion,
            secret,
            algorithms=['HS256'],
            issuer=issuer,
            audience=audience,
            leeway=leeway,
            options={'require': list(_REQUIRED_CLAIMS)},
        )
    except AuthError:
        raise
    except jwt.PyJWTError as exc:
        raise AuthError('Invalid trusted identity assertion') from exc

    oid = claims.get('oid')
    tid = claims.get('tid')
    subject = claims.get('sub')
    issued_at = claims.get('iat')
    expires_at = claims.get('exp')
    if not isinstance(oid, str) or not oid.strip():
        raise AuthError('Invalid trusted identity assertion')
    if not isinstance(tid, str) or not tid.strip():
        raise AuthError('Invalid trusted identity assertion')
    if subject != oid:
        raise AuthError('Invalid trusted identity assertion')
    if not isinstance(issued_at, (int, float)) or not isinstance(
        expires_at, (int, float)
    ):
        raise AuthError('Invalid trusted identity assertion')
    if expires_at <= issued_at or expires_at - issued_at > max_lifetime:
        raise AuthError('Invalid trusted identity assertion lifetime')
    return claims


@dataclass
class TrustedIdentityUserAuth(DefaultUserAuth):
    user_id: str = ''
    user_email: str | None = None

    async def get_user_id(self) -> str:
        return self.user_id

    async def get_user_email(self) -> str | None:
        return self.user_email

    @classmethod
    async def get_instance(cls, request: Request) -> UserAuth:
        header_name = (
            os.getenv('OH_TRUSTED_IDENTITY_ASSERTION_HEADER', _DEFAULT_HEADER).strip()
            or _DEFAULT_HEADER
        )
        assertion = request.headers.get(header_name)
        if not assertion:
            raise AuthError('Trusted identity assertion required')
        claims = decode_identity_assertion(assertion)
        preferred_username = claims.get('preferred_username')
        return cls(
            user_id=f'entra:{claims["tid"]}:{claims["oid"]}',
            user_email=(
                preferred_username
                if isinstance(preferred_username, str) and preferred_username
                else None
            ),
        )

    @classmethod
    async def get_for_user(cls, user_id: str) -> UserAuth:
        prefix, separator, remainder = user_id.partition(':')
        tenant_id, nested_separator, object_id = remainder.partition(':')
        if (
            prefix != 'entra'
            or not separator
            or not nested_separator
            or not tenant_id
            or not object_id
        ):
            raise AuthError('Invalid trusted identity user id')
        return cls(user_id=user_id)
