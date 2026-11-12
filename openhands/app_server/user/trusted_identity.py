from dataclasses import dataclass
from datetime import timedelta
from typing import AsyncGenerator
from uuid import UUID

import jwt
from fastapi import Request
from openhands.agent_server.utils import utc_now
from pydantic import BaseModel, Field, SecretStr

from openhands.app_server.errors import AuthError
from openhands.app_server.services.injector import Injector, InjectorState


@dataclass(frozen=True)
class TrustedIdentity:
    tenant_id: str
    object_id: str


class TrustedIdentityService:
    def __init__(
        self,
        secret: SecretStr,
        issuer: str,
        audience: str,
        header_name: str,
        max_lifetime_seconds: int,
        clock_skew_seconds: int,
    ):
        self.secret = secret
        self.issuer = issuer
        self.audience = audience
        self.header_name = header_name
        self.max_lifetime_seconds = max_lifetime_seconds
        self.clock_skew_seconds = clock_skew_seconds

    def verify_request(self, request: Request) -> TrustedIdentity:
        token = request.headers.get(self.header_name)
        if not token:
            raise AuthError('Missing trusted identity assertion')
        try:
            claims = jwt.decode(
                token,
                self.secret.get_secret_value(),
                algorithms=['HS256'],
                issuer=self.issuer,
                audience=self.audience,
                leeway=timedelta(seconds=self.clock_skew_seconds),
                options={'require': ['aud', 'exp', 'iat', 'iss', 'oid', 'tid']},
            )
        except jwt.InvalidTokenError as exc:
            raise AuthError('Invalid trusted identity assertion') from exc

        issued_at = claims['iat']
        expires_at = claims['exp']
        if not isinstance(issued_at, (int, float)) or not isinstance(
            expires_at, (int, float)
        ):
            raise AuthError('Invalid trusted identity assertion timestamps')
        if expires_at <= issued_at:
            raise AuthError('Invalid trusted identity assertion lifetime')
        if expires_at - issued_at > self.max_lifetime_seconds:
            raise AuthError('Trusted identity assertion lifetime is too long')
        if issued_at > utc_now().timestamp() + self.clock_skew_seconds:
            raise AuthError('Trusted identity assertion was issued in the future')

        try:
            tenant_id = str(UUID(claims['tid']))
            object_id = str(UUID(claims['oid']))
        except (AttributeError, TypeError, ValueError) as exc:
            raise AuthError(
                'Trusted identity assertion must contain Entra UUID claims'
            ) from exc
        return TrustedIdentity(tenant_id=tenant_id, object_id=object_id)


class TrustedIdentityServiceInjector(BaseModel, Injector[TrustedIdentityService]):
    secret: SecretStr = Field(min_length=32)
    issuer: str = 'halo-bff'
    audience: str = 'openhands-sandbox-server'
    header_name: str = 'X-OpenHands-Identity-Assertion'
    max_lifetime_seconds: int = Field(default=300, gt=0, le=900)
    clock_skew_seconds: int = Field(default=30, ge=0, le=300)

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[TrustedIdentityService, None]:
        yield TrustedIdentityService(
            secret=self.secret,
            issuer=self.issuer,
            audience=self.audience,
            header_name=self.header_name,
            max_lifetime_seconds=self.max_lifetime_seconds,
            clock_skew_seconds=self.clock_skew_seconds,
        )
