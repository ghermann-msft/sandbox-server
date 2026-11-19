from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from sqlalchemy import ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.settings.agent_profiles import MAX_AGENT_PROFILES
from openhands.app_server.utils.sql_utils import Base, UtcDateTime
from openhands.sdk.profiles import AgentProfile, validate_agent_profile
from openhands.sdk.profiles.agent_profile_store import (
    PROFILE_NAME_PATTERN,
    PROFILE_NAME_REGEX,
)


class StoredOrganizationAgentProfiles(Base):
    __tablename__ = 'organization_agent_profiles'

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey('organizations.id', ondelete='CASCADE'), primary_key=True
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey('rbac_users.id'), nullable=False
    )
    updated_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey('rbac_users.id'), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OrganizationAgentProfileSeeds(BaseModel):
    model_config = ConfigDict(extra='forbid')

    profiles: dict[str, dict[str, Any]] = Field(max_length=MAX_AGENT_PROFILES)
    active_profile: str
    _validated_profiles: dict[str, AgentProfile] = PrivateAttr(default_factory=dict)

    @model_validator(mode='after')
    def validate_profiles(self) -> OrganizationAgentProfileSeeds:
        validated = {}
        for name, value in self.profiles.items():
            if not PROFILE_NAME_REGEX.fullmatch(name):
                raise ValueError(f'Invalid agent profile seed name: {name!r}')
            validated[name] = validate_agent_profile({**value, 'name': name})
        if self.active_profile not in validated:
            raise ValueError(
                f'Active agent profile seed {self.active_profile!r} is not configured'
            )
        self._validated_profiles = validated
        return self

    @property
    def validated_profiles(self) -> dict[str, AgentProfile]:
        return self._validated_profiles


class AgentProfileInfo(BaseModel):
    id: str | None = None
    name: str
    agent_kind: str = 'openhands'
    revision: int | None = None
    llm_profile_ref: str | None = None
    mcp_server_refs: list[str] | None = None


class AgentProfileListResponse(BaseModel):
    profiles: list[AgentProfileInfo]
    active_agent_profile_id: str | None = None


class AgentProfileDetailResponse(BaseModel):
    name: str
    profile: dict[str, Any]


class AgentProfileMutationResponse(BaseModel):
    name: str
    message: str


class ActivateAgentProfileResponse(BaseModel):
    id: str
    message: str
    agent_settings_applied: bool = False


class RenameAgentProfileRequest(BaseModel):
    new_name: str = Field(
        min_length=1,
        max_length=64,
        pattern=PROFILE_NAME_PATTERN,
    )
