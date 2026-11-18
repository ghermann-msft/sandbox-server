from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.utils.sql_utils import Base, UtcDateTime
from openhands.sdk.mcp.config import MCPServer


class OrganizationConnectionKind(StrEnum):
    GITHUB = 'github'
    ATLASSIAN = 'atlassian'
    PERFORCE = 'perforce'


class StoredOrganizationConnection(Base):
    __tablename__ = 'organization_connections'

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey('organizations.id', ondelete='CASCADE'), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    public_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
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


class MCPConnectionUpsert(BaseModel):
    model_config = ConfigDict(extra='forbid')

    server: MCPServer
    display_name: str | None = Field(default=None, max_length=100)


class OrganizationConnectionStatus(BaseModel):
    kind: OrganizationConnectionKind
    connected: bool
    editable: bool
    scope: str
    display_name: str | None = None
    endpoint: str | None = None
    transport: str | None = None
    migrated_from_user: bool = False


class OrganizationConnectionList(BaseModel):
    organization_id: UUID
    items: list[OrganizationConnectionStatus]
