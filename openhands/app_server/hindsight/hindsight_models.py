from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, model_validator


class HindsightScopeKind(str, Enum):
    PRIVATE = 'private'
    WORKSPACE = 'workspace'
    REPOSITORY = 'repository'
    ORGANIZATION = 'organization'
    SERVER = 'server'


class HindsightReadDepth(str, Enum):
    DEFAULT = 'default'
    ADAPTIVE = 'adaptive'
    DEEP = 'deep'


class HindsightPrincipal(BaseModel):
    tenant_id: UUID
    object_id: UUID

    @property
    def authority_id(self) -> str:
        return f'{self.tenant_id}:{self.object_id}'


class HindsightAuthorizationContext(BaseModel):
    tenant_id: UUID
    object_id: UUID
    organization_id: str | None = None
    role: str | None = None
    permissions: set[str] = Field(default_factory=set)

    @property
    def principal(self) -> HindsightPrincipal:
        return HindsightPrincipal(
            tenant_id=self.tenant_id,
            object_id=self.object_id,
        )


class HindsightScopeRequest(BaseModel):
    type: HindsightScopeKind = Field(validation_alias=AliasChoices('type', 'kind'))
    id: str | None = None
    repository: str | None = None

    @model_validator(mode='before')
    @classmethod
    def canonicalize_private(cls, value):
        if (
            isinstance(value, dict)
            and value.get('type', value.get('kind')) == 'private_user'
        ):
            value = dict(value)
            if 'type' in value:
                value['type'] = HindsightScopeKind.PRIVATE
            else:
                value['kind'] = HindsightScopeKind.PRIVATE
        return value

    @model_validator(mode='after')
    def validate_repository(self):
        if self.type != HindsightScopeKind.REPOSITORY and self.repository is not None:
            raise ValueError('repository is only valid for repository scope')
        return self


class ResolvedHindsightScope(BaseModel):
    type: HindsightScopeKind
    id: str


class HindsightOrganizationBinding(BaseModel):
    display_name: str
    kind: Literal['personal', 'shared']


class HindsightRepositoryScopeBinding(BaseModel):
    organization_ids: list[str] = Field(default_factory=list)


class HindsightScopeBindings(BaseModel):
    organizations: dict[str, HindsightOrganizationBinding] = Field(default_factory=dict)
    repositories: dict[str, HindsightRepositoryScopeBinding] = Field(
        default_factory=dict
    )


class HindsightReadRequest(BaseModel):
    conversation_id: UUID
    query: Annotated[str, Field(min_length=1, max_length=4000)]
    scope: Annotated[list[HindsightScopeRequest], Field(min_length=1, max_length=6)] = (
        Field(validation_alias=AliasChoices('scope', 'scopes'))
    )
    max_results: Annotated[int, Field(ge=1, le=20)] = 10
    depth: HindsightReadDepth = HindsightReadDepth.DEFAULT


class HindsightSource(BaseModel):
    untrusted: Literal[True] = True
    source_memory_id: str
    source_event_id: str
    source_conversation_id: str
    date: datetime
    scope: ResolvedHindsightScope
    uncertainty: Annotated[float, Field(ge=0, le=1)] = 1.0
    content: str


class HindsightRecallResponse(BaseModel):
    available: bool
    memories: list[HindsightSource] = Field(default_factory=list)


class HindsightReflectResponse(BaseModel):
    available: bool
    text: str | None = None
    sources: list[HindsightSource] = Field(default_factory=list)


class HindsightRetainApprovalRequest(BaseModel):
    conversation_id: UUID
    source_event_id: Annotated[str, Field(min_length=1, max_length=255)]
    scope: HindsightScopeRequest
    content_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class HindsightRetainApprovalResponse(BaseModel):
    approval_token: str


class HindsightRetainRequest(BaseModel):
    conversation_id: UUID
    source_event_id: Annotated[str, Field(min_length=1, max_length=255)]
    scope: HindsightScopeRequest
    content: Annotated[str, Field(min_length=1, max_length=100_000)]
    occurred_at: datetime
    approval_token: Annotated[str, Field(min_length=1)] | None = None


class HindsightRetainResponse(BaseModel):
    event_id: str
    status: Literal['queued', 'delivered', 'dead_letter']
    duplicate: bool = False
