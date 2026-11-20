from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class AgentServerMemoryScope(BaseModel):
    model_config = ConfigDict(extra='forbid')

    type: Literal[
        'server',
        'organization',
        'repository',
        'workspace',
        'private_user',
    ]
    id: str = Field(min_length=1)


class AgentServerHindsightMemory(BaseModel):
    model_config = ConfigDict(extra='forbid')

    broker_url: HttpUrl
    identity: str = Field(min_length=1)
    scope: list[AgentServerMemoryScope] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30.0, gt=0, le=30)


class AgentServerInitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_api_keys: list[str] | None = None
    secret_key: str | None = None
    hindsight_memory: AgentServerHindsightMemory | None = None


class AgentServerInitStatus(BaseModel):
    state: Literal['dormant', 'initializing', 'ready']
    error: str | None = None
