from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cache
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.organization.agent_profile_models import (
    OrganizationAgentProfileSeeds,
    StoredOrganizationAgentProfiles,
)
from openhands.app_server.organization.organization_models import (
    OrganizationPermission,
)
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.settings.agent_profiles import (
    MAX_AGENT_PROFILES,
    AgentProfiles,
)
from openhands.app_server.user.user_context import UserContext
from openhands.sdk.profiles import (
    AgentProfile,
    ProfileLimitExceeded,
    save_profile_preserving_identity,
)

_MANAGE_PERMISSIONS = {
    OrganizationPermission.ORG_MANAGE,
    OrganizationPermission.ORG_MANAGE_MEMBERS,
}
_SEEDS_ENV = 'OH_ORGANIZATION_AGENT_PROFILE_SEEDS_JSON'


@cache
def get_organization_agent_profile_seeds() -> OrganizationAgentProfileSeeds | None:
    value = os.getenv(_SEEDS_ENV)
    if value is None:
        return None
    return OrganizationAgentProfileSeeds.model_validate_json(value)


@dataclass
class OrganizationAgentProfileService:
    db_session: AsyncSession
    user_context: UserContext
    seeds: OrganizationAgentProfileSeeds | None = field(
        default_factory=get_organization_agent_profile_seeds
    )

    @property
    def organizations(self) -> OrganizationService:
        return OrganizationService(self.db_session, self.user_context)

    async def _authorize(self, organization_id: UUID, *, manage: bool) -> None:
        if manage:
            await self.organizations.require_any_permission(
                organization_id, _MANAGE_PERMISSIONS
            )
        else:
            await self.organizations.require_permission(
                organization_id, OrganizationPermission.ORG_VIEW
            )

    async def _row(
        self, organization_id: UUID, *, for_update: bool = False
    ) -> StoredOrganizationAgentProfiles | None:
        query = select(StoredOrganizationAgentProfiles).where(
            StoredOrganizationAgentProfiles.organization_id == organization_id
        )
        if for_update:
            query = query.with_for_update()
        return await self.db_session.scalar(query)

    @staticmethod
    def _deserialize(row: StoredOrganizationAgentProfiles) -> AgentProfiles:
        return AgentProfiles.model_validate_json(row.payload)

    def _initial_profiles(self, organization_id: UUID) -> AgentProfiles:
        profiles = AgentProfiles()
        if self.seeds is None:
            return profiles
        ids_by_name = {}
        for name, profile in self.seeds.validated_profiles.items():
            profile_id = uuid5(organization_id, name)
            profiles.save(
                profile.model_copy(update={'id': profile_id, 'revision': 0}),
                max_profiles=MAX_AGENT_PROFILES,
            )
            ids_by_name[name] = str(profile_id)
        profiles.active = ids_by_name[self.seeds.active_profile]
        return profiles

    async def load(self, organization_id: UUID) -> AgentProfiles:
        await self._authorize(organization_id, manage=False)
        row = await self._row(organization_id)
        if row is not None:
            return self._deserialize(row)
        profiles = self._initial_profiles(organization_id)
        if self.seeds is not None:
            await self._store(organization_id, row, profiles)
        return profiles

    async def _mutate(
        self, organization_id: UUID
    ) -> tuple[StoredOrganizationAgentProfiles | None, AgentProfiles]:
        await self._authorize(organization_id, manage=True)
        row = await self._row(organization_id, for_update=True)
        return (
            row,
            self._deserialize(row)
            if row is not None
            else self._initial_profiles(organization_id),
        )

    async def _store(
        self,
        organization_id: UUID,
        row: StoredOrganizationAgentProfiles | None,
        profiles: AgentProfiles,
    ) -> None:
        user = await self.organizations._user()
        payload = profiles.model_dump_json()
        if row is None:
            self.db_session.add(
                StoredOrganizationAgentProfiles(
                    organization_id=organization_id,
                    payload=payload,
                    created_by_user_id=user.id,
                    updated_by_user_id=user.id,
                )
            )
        else:
            row.payload = payload
            row.updated_by_user_id = user.id
            row.revision += 1
        await self.db_session.flush()

    async def save(
        self,
        organization_id: UUID,
        profile: AgentProfile,
    ) -> AgentProfiles:
        row, profiles = await self._mutate(organization_id)
        save_profile_preserving_identity(
            profiles,
            profile,
            max_profiles=MAX_AGENT_PROFILES,
        )
        await self._store(organization_id, row, profiles)
        return profiles

    async def delete(self, organization_id: UUID, name: str) -> AgentProfiles:
        row, profiles = await self._mutate(organization_id)
        profiles.delete(name)
        if row is not None:
            await self._store(organization_id, row, profiles)
        return profiles

    async def rename(
        self, organization_id: UUID, name: str, new_name: str
    ) -> AgentProfiles:
        row, profiles = await self._mutate(organization_id)
        profiles.rename(name, new_name)
        await self._store(organization_id, row, profiles)
        return profiles

    async def activate(self, organization_id: UUID, profile_id: str) -> AgentProfiles:
        row, profiles = await self._mutate(organization_id)
        if not any(
            str(profile.id) == profile_id for profile in profiles.profiles.values()
        ):
            raise FileNotFoundError(profile_id)
        profiles.active = profile_id
        await self._store(organization_id, row, profiles)
        return profiles


__all__ = ['OrganizationAgentProfileService', 'ProfileLimitExceeded']
