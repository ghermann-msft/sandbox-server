from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.organization.organization_models import (
    OrganizationKind,
    OrganizationPermission,
    StoredOrganization,
)
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.organization.profile_models import (
    OrganizationProfileSaveRequest,
    StoredOrganizationProfiles,
)
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.settings.llm_profiles import (
    LLMProfiles,
    ProfileAlreadyExistsError,
    ProfileLimitExceededError,
    ProfileNotFoundError,
)
from openhands.app_server.user.user_context import UserContext

_MANAGE_PERMISSIONS = {
    OrganizationPermission.ORG_MANAGE,
    OrganizationPermission.ORG_MANAGE_MEMBERS,
}


@dataclass
class OrganizationProfileService:
    db_session: AsyncSession
    user_context: UserContext
    jwt_service: JwtService

    @property
    def organizations(self) -> OrganizationService:
        return OrganizationService(self.db_session, self.user_context)

    async def _authorize(
        self,
        organization_id: UUID,
        *,
        manage: bool,
        shared_required: bool = True,
    ) -> bool:
        if manage:
            await self.organizations.require_any_permission(
                organization_id, _MANAGE_PERMISSIONS
            )
        else:
            await self.organizations.require_permission(
                organization_id, OrganizationPermission.ORG_VIEW
            )
        organization = await self.db_session.get(StoredOrganization, organization_id)
        if organization is None:
            raise RuntimeError(
                'Organization membership references a missing organization'
            )
        is_shared = organization.kind == OrganizationKind.SHARED.value
        if shared_required and not is_shared:
            raise ValueError('Personal profiles use the personal settings API')
        return is_shared

    async def _row(
        self, organization_id: UUID, *, for_update: bool = False
    ) -> StoredOrganizationProfiles | None:
        query = select(StoredOrganizationProfiles).where(
            StoredOrganizationProfiles.organization_id == organization_id
        )
        if for_update:
            query = query.with_for_update()
        return await self.db_session.scalar(query)

    def _decrypt(self, row: StoredOrganizationProfiles) -> LLMProfiles:
        return LLMProfiles.model_validate_json(
            self.jwt_service.decrypt_value(row.encrypted_payload)
        )

    def _encrypt(self, profiles: LLMProfiles) -> str:
        return self.jwt_service.encrypt_value(
            profiles.model_dump_json(context={'expose_secrets': True})
        )

    async def load(
        self, organization_id: UUID, *, shared_required: bool = True
    ) -> LLMProfiles:
        is_shared = await self._authorize(
            organization_id, manage=False, shared_required=shared_required
        )
        if not is_shared:
            return LLMProfiles()
        row = await self._row(organization_id)
        return self._decrypt(row) if row is not None else LLMProfiles()

    async def _mutate(
        self, organization_id: UUID
    ) -> tuple[StoredOrganizationProfiles | None, LLMProfiles]:
        await self._authorize(organization_id, manage=True)
        row = await self._row(organization_id, for_update=True)
        return row, self._decrypt(row) if row is not None else LLMProfiles()

    async def _store(
        self,
        organization_id: UUID,
        row: StoredOrganizationProfiles | None,
        profiles: LLMProfiles,
    ) -> None:
        user = await self.organizations._user()
        encrypted_payload = self._encrypt(profiles)
        if row is None:
            self.db_session.add(
                StoredOrganizationProfiles(
                    organization_id=organization_id,
                    encrypted_payload=encrypted_payload,
                    created_by_user_id=user.id,
                    updated_by_user_id=user.id,
                )
            )
        else:
            row.encrypted_payload = encrypted_payload
            row.updated_by_user_id = user.id
            row.revision += 1
        await self.db_session.flush()

    async def save(
        self,
        organization_id: UUID,
        name: str,
        request: OrganizationProfileSaveRequest,
    ) -> LLMProfiles:
        row, profiles = await self._mutate(organization_id)
        existing = profiles.get(name)
        llm = request.llm
        if request.preserve_existing_api_key and existing is not None:
            llm = llm.model_copy(update={'api_key': existing.api_key})
        elif llm.api_key is None and existing is not None:
            llm = llm.model_copy(update={'api_key': existing.api_key})
        profiles.save(name, llm, include_secrets=request.include_secrets)
        await self._store(organization_id, row, profiles)
        return profiles

    async def delete(self, organization_id: UUID, name: str) -> None:
        row, profiles = await self._mutate(organization_id)
        if row is not None and profiles.delete(name):
            await self._store(organization_id, row, profiles)

    async def activate(self, organization_id: UUID, name: str) -> LLMProfiles:
        row, profiles = await self._mutate(organization_id)
        profiles.require(name)
        profiles.active = name
        await self._store(organization_id, row, profiles)
        return profiles

    async def rename(
        self, organization_id: UUID, name: str, new_name: str
    ) -> LLMProfiles:
        row, profiles = await self._mutate(organization_id)
        profiles.rename(name, new_name)
        await self._store(organization_id, row, profiles)
        return profiles


__all__ = [
    'OrganizationProfileService',
    'ProfileAlreadyExistsError',
    'ProfileLimitExceededError',
    'ProfileNotFoundError',
]
