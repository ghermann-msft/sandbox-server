from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from openhands.agent_server.utils import utc_now
from openhands.app_server.settings.llm_profiles import StrictLLM
from openhands.app_server.utils.sql_utils import Base


class StoredOrganizationProfiles(Base):
    __tablename__ = 'organization_llm_profiles'

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey('organizations.id', ondelete='CASCADE'), primary_key=True
    )
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
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


class OrganizationProfileInfo(BaseModel):
    name: str
    model: str | None = None
    base_url: str | None = None
    api_key_set: bool = False


class OrganizationProfileListResponse(BaseModel):
    profiles: list[OrganizationProfileInfo]
    active_profile: str | None = None


class OrganizationProfileDetailResponse(BaseModel):
    name: str
    config: dict[str, Any]
    api_key_set: bool = False


class OrganizationProfileMutationResponse(BaseModel):
    name: str
    message: str


class OrganizationProfileActivateResponse(BaseModel):
    name: str
    message: str
    model: str | None = None


class OrganizationProfileRenameRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    new_name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$',
    )


class OrganizationProfileSaveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    include_secrets: bool = True
    llm: StrictLLM
    preserve_existing_api_key: bool = False
