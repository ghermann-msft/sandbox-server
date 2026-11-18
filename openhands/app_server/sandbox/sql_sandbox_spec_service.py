from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from openhands.agent_server.utils import utc_now
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxSpecAvailability,
    SandboxSpecCreate,
    SandboxSpecInfo,
    SandboxSpecInfoPage,
    SandboxSpecUpdate,
)
from openhands.app_server.sandbox.sandbox_spec_service import SandboxSpecService
from openhands.app_server.utils.sql_utils import Base


class StoredSandboxSpec(Base):
    __tablename__ = 'v1_sandbox_spec'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


@dataclass
class SQLSandboxSpecService(SandboxSpecService):
    db_session: AsyncSession

    async def seed_sandbox_specs(self, specs: list[SandboxSpecInfo]) -> None:
        for spec in specs:
            profile = spec.model_copy(update={'source': 'bootstrap', 'immutable': True})
            stored = await self.db_session.get(StoredSandboxSpec, profile.id)
            if stored is None:
                self.db_session.add(
                    StoredSandboxSpec(
                        id=profile.id,
                        payload=profile.model_dump(mode='json'),
                        created_at=profile.created_at,
                        updated_at=profile.updated_at,
                    )
                )
            else:
                existing = SandboxSpecInfo.model_validate(stored.payload)
                if existing.source == 'bootstrap':
                    candidate = profile.model_copy(
                        update={
                            'created_at': existing.created_at,
                            'updated_at': existing.updated_at,
                        }
                    )
                    if candidate.model_dump(mode='json') == existing.model_dump(
                        mode='json'
                    ):
                        continue
                    profile = candidate.model_copy(update={'updated_at': utc_now()})
                    stored.payload = profile.model_dump(mode='json')
                    stored.updated_at = profile.updated_at
        await self.db_session.commit()

    async def _all(self) -> list[SandboxSpecInfo]:
        rows = (
            (
                await self.db_session.execute(
                    select(StoredSandboxSpec).order_by(StoredSandboxSpec.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [SandboxSpecInfo.model_validate(row.payload) for row in rows]

    @staticmethod
    def _selectable(spec: SandboxSpecInfo) -> bool:
        return (
            spec.enabled
            and spec.published
            and spec.availability == SandboxSpecAvailability.AVAILABLE
        )

    @staticmethod
    def _page(
        specs: list[SandboxSpecInfo], page_id: str | None, limit: int
    ) -> SandboxSpecInfoPage:
        try:
            offset = max(int(page_id), 0) if page_id else 0
        except ValueError:
            offset = 0
        items = specs[offset : offset + limit]
        next_page_id = str(offset + limit) if len(specs) > offset + limit else None
        return SandboxSpecInfoPage(items=items, next_page_id=next_page_id)

    async def search_sandbox_specs(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxSpecInfoPage:
        specs = [spec for spec in await self._all() if self._selectable(spec)]
        return self._page(specs, page_id, limit)

    async def search_all_sandbox_specs(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxSpecInfoPage:
        return self._page(await self._all(), page_id, limit)

    async def get_sandbox_spec(
        self, sandbox_spec_id: str, include_unavailable: bool = False
    ) -> SandboxSpecInfo | None:
        stored = await self.db_session.get(StoredSandboxSpec, sandbox_spec_id)
        if stored is None:
            return None
        spec = SandboxSpecInfo.model_validate(stored.payload)
        if include_unavailable or self._selectable(spec):
            return spec
        return None

    async def get_default_sandbox_spec(self) -> SandboxSpecInfo:
        page = await self.search_sandbox_specs(limit=1)
        if not page.items:
            from openhands.app_server.errors import SandboxError

            raise SandboxError('No sandbox specs available!')
        return page.items[0]

    async def create_sandbox_spec(self, request: SandboxSpecCreate) -> SandboxSpecInfo:
        if await self.db_session.get(StoredSandboxSpec, request.profile.id):
            raise ValueError(f'Sandbox Spec {request.profile.id!r} already exists')
        profile = request.profile.model_copy(
            update={
                'source': 'admin',
                'immutable': False,
                'bootstrap_revision': None,
                'created_at': utc_now(),
                'updated_at': utc_now(),
            }
        )
        self.db_session.add(
            StoredSandboxSpec(
                id=profile.id,
                payload=profile.model_dump(mode='json'),
                created_at=profile.created_at,
                updated_at=profile.updated_at,
            )
        )
        await self.db_session.commit()
        return profile

    async def update_sandbox_spec(
        self, sandbox_spec_id: str, request: SandboxSpecUpdate
    ) -> SandboxSpecInfo | None:
        stored = await self.db_session.get(StoredSandboxSpec, sandbox_spec_id)
        if stored is None:
            return None
        existing = SandboxSpecInfo.model_validate(stored.payload)
        if existing.immutable:
            raise ValueError(
                f'Sandbox Spec {sandbox_spec_id!r} is bootstrap-managed and immutable'
            )
        profile = SandboxSpecInfo.model_validate(
            {
                **existing.model_dump(),
                **request.model_dump(exclude_unset=True),
                'updated_at': utc_now(),
            }
        )
        stored.payload = profile.model_dump(mode='json')
        stored.updated_at = profile.updated_at
        await self.db_session.commit()
        return profile

    async def delete_sandbox_spec(self, sandbox_spec_id: str) -> bool:
        stored = await self.db_session.get(StoredSandboxSpec, sandbox_spec_id)
        if stored is None:
            return False
        existing = SandboxSpecInfo.model_validate(stored.payload)
        if existing.immutable:
            raise ValueError(
                f'Sandbox Spec {sandbox_spec_id!r} is bootstrap-managed and immutable'
            )
        profile = existing.model_copy(
            update={
                'enabled': False,
                'published': False,
                'availability': SandboxSpecAvailability.DISABLED,
                'availability_reason': 'Deleted by an administrator',
                'updated_at': utc_now(),
            }
        )
        stored.payload = profile.model_dump(mode='json')
        stored.updated_at = profile.updated_at
        await self.db_session.commit()
        return True
