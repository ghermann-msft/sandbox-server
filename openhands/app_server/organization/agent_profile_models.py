from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from openhands.agent_server.utils import utc_now
from openhands.app_server.utils.sql_utils import Base
from openhands.sdk.profiles.agent_profile_store import PROFILE_NAME_PATTERN


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
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=utc_now, onupdate=utc_now
    )


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
