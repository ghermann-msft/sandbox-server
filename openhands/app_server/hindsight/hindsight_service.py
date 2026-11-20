from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException, status
from sqlalchemy import JSON, Integer, String, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from openhands.agent_server.utils import utc_now
from openhands.app_server.hindsight.hindsight_config import (
    read_hindsight_secret_file,
)
from openhands.app_server.hindsight.hindsight_models import (
    HindsightAuthorizationContext,
    HindsightPrincipal,
    HindsightProvenance,
    HindsightScopeBindings,
    HindsightScopeKind,
    HindsightScopeRequest,
    HindsightSource,
    ResolvedHindsightScope,
)
from openhands.app_server.utils.sql_utils import Base, UtcDateTime

_logger = logging.getLogger(__name__)


class HindsightOutboxStatus:
    PENDING = 'pending'
    DELIVERING = 'delivering'
    RETRY = 'retry'
    DELIVERED = 'delivered'
    DEAD_LETTER = 'dead_letter'


class StoredHindsightOutbox(Base):
    __tablename__ = 'hindsight_outbox'

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    bank_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=HindsightOutboxStatus.PENDING, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, index=True
    )
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )


@dataclass(frozen=True)
class HindsightBrokerSettings:
    base_url: str
    api_key: str | None = None
    api_key_file: Path | None = None
    bank_secret: str | None = None
    bank_secret_file: Path | None = None
    fail_open: bool = True
    max_outbox_pending: int = 10_000
    max_attempts: int = 8
    request_timeout_seconds: float = 5.0
    reflect_timeout_seconds: float = 25.0

    def get_api_key(self) -> str:
        if self.api_key_file is not None:
            return read_hindsight_secret_file(self.api_key_file)
        if self.api_key:
            return self.api_key
        raise ValueError('Hindsight API key is not configured')

    def get_bank_secret(self) -> str:
        if self.bank_secret_file is not None:
            return read_hindsight_secret_file(self.bank_secret_file)
        if self.bank_secret:
            return self.bank_secret
        raise ValueError('Hindsight bank secret is not configured')


def normalize_repository(value: str) -> str:
    repository = value.strip().lower()
    if repository.endswith('.git'):
        repository = repository[:-4]
    if '://' in repository:
        repository = repository.split('://', 1)[1]
    if (
        '@' in repository
        and ':' in repository
        and '/' not in repository.split(':', 1)[0]
    ):
        repository = repository.split(':', 1)[1]
    repository = repository.split('?', 1)[0].split('#', 1)[0].strip('/')
    parts = repository.split('/')
    if len(parts) < 2:
        raise ValueError('repository must identify an owner and repository')
    return '/'.join(parts[-2:])


def resolve_scope(
    request: HindsightScopeRequest,
    principal: HindsightPrincipal,
    sandbox_id: str,
    selected_repository: str | None,
    git_provider: str | None = None,
    deployment_id: str | None = None,
    scope_bindings: HindsightScopeBindings | None = None,
) -> ResolvedHindsightScope:
    if request.type == HindsightScopeKind.PRIVATE:
        scope_id = principal.authority_id
    elif request.type == HindsightScopeKind.WORKSPACE:
        scope_id = sandbox_id
    elif request.type == HindsightScopeKind.SERVER:
        if not deployment_id:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail='Server memory scope is not configured',
            )
        scope_id = deployment_id
    else:
        if not selected_repository:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail='Conversation has no authorized repository scope',
            )
        try:
            authorized = normalize_repository(selected_repository)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail='Invalid repository scope'
            ) from exc
        repository_id = f'{git_provider or "unknown"}:{authorized}'
        if request.type == HindsightScopeKind.REPOSITORY:
            requested_repository = request.repository or request.id
            if not requested_repository:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    detail='Repository scope ID is required',
                )
            if requested_repository == repository_id:
                requested = authorized
            else:
                try:
                    requested = normalize_repository(requested_repository)
                except ValueError as exc:
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN, detail='Invalid repository scope'
                    ) from exc
            if requested != authorized:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    detail='Repository scope is not authorized for this conversation',
                )
            scope_id = repository_id
        else:
            requested_scope_id = request.id
            if requested_scope_id is None:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    detail='Organization scope ID is required',
                )
            scope_id = requested_scope_id
            if request.type != HindsightScopeKind.ORGANIZATION:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail='Unsupported memory scope',
                )
            repository_binding = (
                scope_bindings.repositories.get(repository_id)
                if scope_bindings is not None
                else None
            )
            if (
                scope_bindings is None
                or scope_id not in scope_bindings.organizations
                or repository_binding is None
                or scope_id not in repository_binding.organization_ids
            ):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    detail=(
                        'Repository is not authorized for the requested '
                        'organization memory scope'
                    ),
                )
    if request.id is not None and request.id != scope_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Caller-supplied memory scope ID is not authorized',
        )
    return ResolvedHindsightScope(type=request.type, id=scope_id)


def authorize_scope_operation(
    authorization: HindsightAuthorizationContext,
    scope: ResolvedHindsightScope,
    permission: str,
) -> None:
    if scope.type != HindsightScopeKind.ORGANIZATION:
        return
    if (
        authorization.organization_id != scope.id
        or permission not in authorization.permissions
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Organization memory scope is not authorized by OpenHands RBAC',
        )


def authorize_conversation_organization(
    conversation_organization_id: str | None,
    authorization: HindsightAuthorizationContext,
    scopes: list[ResolvedHindsightScope],
) -> None:
    if not any(scope.type == HindsightScopeKind.ORGANIZATION for scope in scopes):
        return
    if (
        conversation_organization_id is None
        or authorization.organization_id != conversation_organization_id
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Organization memory requires the trusted conversation organization',
        )


def derive_bank_id(
    principal: HindsightPrincipal,
    scope: ResolvedHindsightScope,
    bank_secret: str,
) -> str:
    if scope.type == HindsightScopeKind.PRIVATE:
        authority = f'{principal.tenant_id}:{principal.object_id}'
    elif scope.type in {
        HindsightScopeKind.SERVER,
        HindsightScopeKind.ORGANIZATION,
    }:
        authority = f'{scope.type.value}:{scope.id}'
    else:
        authority = f'{principal.tenant_id}:{scope.type.value}:{scope.id}'
    return hmac.new(
        bank_secret.encode(), authority.encode(), hashlib.sha256
    ).hexdigest()


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def derive_event_id(
    principal: HindsightPrincipal,
    conversation_id: str,
    source_event_id: str,
    scope: ResolvedHindsightScope,
    content_digest: str,
) -> str:
    value = json.dumps(
        {
            'tenant_id': str(principal.tenant_id),
            'object_id': str(principal.object_id),
            'conversation_id': conversation_id,
            'source_event_id': source_event_id,
            'scope': scope.model_dump(mode='json'),
            'content_sha256': content_digest,
        },
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def validate_retain_approval(
    approval: dict[str, Any],
    *,
    user_id: str,
    principal: HindsightPrincipal,
    conversation_id: str,
    sandbox_id: str,
    source_event_id: str,
    scope: ResolvedHindsightScope,
    content_digest: str,
) -> None:
    expected = {
        'purpose': 'hindsight_retain_approval',
        'user_id': user_id,
        'tenant_id': str(principal.tenant_id),
        'object_id': str(principal.object_id),
        'conversation_id': conversation_id,
        'sandbox_id': sandbox_id,
        'source_event_id': source_event_id,
        'scope': scope.model_dump(mode='json'),
        'content_sha256': content_digest,
    }
    if any(approval.get(key) != value for key, value in expected.items()):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Retain approval does not match this event',
        )


def retain_requires_approval(scope: ResolvedHindsightScope) -> bool:
    return scope.type not in {
        HindsightScopeKind.PRIVATE,
        HindsightScopeKind.SERVER,
        HindsightScopeKind.ORGANIZATION,
    }


def authorize_explicit_retain_scope(scope: ResolvedHindsightScope) -> None:
    if scope.type != HindsightScopeKind.PRIVATE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Explicit retain is restricted to private_user memory',
        )


class HindsightBroker:
    def __init__(
        self,
        settings: HindsightBrokerSettings,
        async_session_maker: async_sessionmaker,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.async_session_maker = async_session_maker
        self.http_client = http_client

    def _headers(self, event_id: str | None = None) -> dict[str, str]:
        headers = {'Authorization': f'Bearer {self.settings.get_api_key()}'}
        if event_id:
            headers['Idempotency-Key'] = event_id
        return headers

    @staticmethod
    def _is_transient(response: httpx.Response) -> bool:
        return response.status_code in {408, 429} or response.status_code >= 500

    async def _client(self):
        if self.http_client is not None:
            return self.http_client, False
        return (
            httpx.AsyncClient(timeout=self.settings.request_timeout_seconds),
            True,
        )

    async def recall(
        self,
        query: str,
        scopes: list[tuple[str, ResolvedHindsightScope]],
        max_results: int,
        token_budget: int,
    ) -> tuple[bool, list[HindsightSource]]:
        client, close = await self._client()
        try:
            calls = [
                client.post(
                    f'{self.settings.base_url.rstrip("/")}/v1/default/banks/{quote(bank_id)}/memories/recall',
                    headers=self._headers(),
                    json={'query': query, 'max_tokens': token_budget, 'budget': 'low'},
                )
                for bank_id, _ in scopes
            ]
            responses = await asyncio.gather(*calls, return_exceptions=True)
            sources: list[HindsightSource] = []
            available = False
            for response, (_, scope) in zip(responses, scopes, strict=True):
                if isinstance(response, BaseException):
                    if not self.settings.fail_open:
                        raise response
                    continue
                if response.status_code == 404:
                    available = True
                    continue
                if self._is_transient(response):
                    if not self.settings.fail_open:
                        response.raise_for_status()
                    continue
                response.raise_for_status()
                available = True
                for item in response.json().get('results', []):
                    metadata = item.get('metadata') or {}
                    date = item.get('occurred_start') or item.get('mentioned_at')
                    source_event_id = metadata.get('source_event_id')
                    source_conversation_id = metadata.get('source_conversation_id')
                    if not date or not source_event_id or not source_conversation_id:
                        continue
                    score = (item.get('scores') or {}).get('final') or 0.0
                    sources.append(
                        HindsightSource(
                            source_memory_id=str(item['id']),
                            source_event_id=source_event_id,
                            source_conversation_id=source_conversation_id,
                            date=date,
                            scope=(scope,),
                            relevance=max(0.0, min(1.0, float(score))),
                            uncertainty=max(0.0, min(1.0, 1.0 - float(score))),
                            content=item['text'],
                        )
                    )
            sources.sort(key=lambda source: source.relevance, reverse=True)
            return available, sources[:max_results]
        finally:
            if close:
                await client.aclose()

    async def reflect(
        self,
        query: str,
        scopes: list[tuple[str, ResolvedHindsightScope]],
        token_budget: int,
    ) -> tuple[bool, str | None, list[HindsightProvenance]]:
        client, close = await self._client()
        try:
            calls = [
                client.post(
                    f'{self.settings.base_url.rstrip("/")}/v1/default/banks/{quote(bank_id)}/reflect',
                    headers=self._headers(),
                    timeout=self.settings.reflect_timeout_seconds,
                    json={
                        'query': query,
                        'max_tokens': token_budget,
                        'budget': 'low',
                        'include': {'facts': {}},
                    },
                )
                for bank_id, _ in scopes
            ]
            responses = await asyncio.gather(*calls, return_exceptions=True)
            texts = []
            sources: list[HindsightProvenance] = []
            available = False
            for response, (_, scope) in zip(responses, scopes, strict=True):
                if isinstance(response, BaseException):
                    if not self.settings.fail_open:
                        raise response
                    continue
                if response.status_code == 404:
                    available = True
                    continue
                if self._is_transient(response):
                    if not self.settings.fail_open:
                        response.raise_for_status()
                    continue
                response.raise_for_status()
                available = True
                payload = response.json()
                if payload.get('text'):
                    texts.append(payload['text'])
                based_on = payload.get('based_on') or {}
                for item in based_on.get('memories') or []:
                    if not item.get('id'):
                        continue
                    metadata = item.get('metadata') or {}
                    date = item.get('occurred_start') or item.get('mentioned_at')
                    source_event_id = metadata.get('source_event_id')
                    source_conversation_id = metadata.get('source_conversation_id')
                    if not date or not source_event_id or not source_conversation_id:
                        continue
                    score = (item.get('scores') or {}).get('final')
                    relevance = (
                        max(0.0, min(1.0, float(score))) if score is not None else 0.0
                    )
                    sources.append(
                        HindsightProvenance(
                            source_memory_id=str(item['id']),
                            source_event_id=source_event_id,
                            source_conversation_id=source_conversation_id,
                            date=date,
                            scope=(scope,),
                            relevance=relevance,
                            uncertainty=max(0.0, min(1.0, 1.0 - relevance)),
                        )
                    )
            return available, '\n\n'.join(texts) or None, sources
        finally:
            if close:
                await client.aclose()

    async def enqueue(
        self, event_id: str, bank_id: str, payload: dict[str, Any]
    ) -> tuple[StoredHindsightOutbox, bool]:
        async with self.async_session_maker() as session:
            existing = await session.get(StoredHindsightOutbox, event_id)
            if existing is not None:
                return existing, True
            pending_count = await session.scalar(
                select(func.count())
                .select_from(StoredHindsightOutbox)
                .where(
                    StoredHindsightOutbox.status.in_(
                        [
                            HindsightOutboxStatus.PENDING,
                            HindsightOutboxStatus.RETRY,
                            HindsightOutboxStatus.DELIVERING,
                        ]
                    )
                )
            )
            if (pending_count or 0) >= self.settings.max_outbox_pending:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail='Hindsight retain queue is at capacity',
                )
            row = StoredHindsightOutbox(
                event_id=event_id, bank_id=bank_id, payload=payload
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.get(StoredHindsightOutbox, event_id)
                assert existing is not None
                return existing, True
            return row, False

    async def deliver_once(self) -> bool:
        now = utc_now()
        async with self.async_session_maker() as session:
            row = await session.scalar(
                select(StoredHindsightOutbox)
                .where(
                    or_(
                        StoredHindsightOutbox.status.in_(
                            [
                                HindsightOutboxStatus.PENDING,
                                HindsightOutboxStatus.RETRY,
                            ]
                        ),
                        StoredHindsightOutbox.status
                        == HindsightOutboxStatus.DELIVERING,
                    ),
                    StoredHindsightOutbox.next_attempt_at <= now,
                )
                .order_by(StoredHindsightOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            row.status = HindsightOutboxStatus.DELIVERING
            row.next_attempt_at = now + timedelta(seconds=60)
            row.updated_at = now
            await session.commit()
            event_id = row.event_id
            bank_id = row.bank_id
            payload = row.payload

        error: str | None = None
        client, close = await self._client()
        try:
            response = await client.post(
                f'{self.settings.base_url.rstrip("/")}/v1/default/banks/{quote(bank_id)}/memories',
                headers=self._headers(event_id),
                json={
                    'items': [payload],
                    'async': True,
                    'operation_id': event_id,
                },
            )
            response.raise_for_status()
        except Exception as exc:
            error = str(exc)[:2000]
        finally:
            if close:
                await client.aclose()

        async with self.async_session_maker() as session:
            row = await session.get(StoredHindsightOutbox, event_id)
            if row is None:
                return True
            row.attempt_count += 1
            row.updated_at = utc_now()
            if error is None:
                row.status = HindsightOutboxStatus.DELIVERED
                row.last_error = None
            elif row.attempt_count >= self.settings.max_attempts:
                row.status = HindsightOutboxStatus.DEAD_LETTER
                row.last_error = error
            else:
                row.status = HindsightOutboxStatus.RETRY
                row.last_error = error
                delay = min(300, 2 ** min(row.attempt_count, 8))
                row.next_attempt_at = utc_now() + timedelta(seconds=delay)
            await session.commit()
        return True


async def run_hindsight_outbox_worker(broker: HindsightBroker, stop: asyncio.Event):
    while not stop.is_set():
        try:
            delivered = await broker.deliver_once()
        except Exception:
            _logger.exception('Hindsight outbox worker failed')
            delivered = False
        if not delivered:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass
