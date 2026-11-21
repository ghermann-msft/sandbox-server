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


class AgentServerWebhookSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')

    base_url: str


class AgentServerInitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_api_keys: list[str] | None = None
    secret_key: str | None = None
    previous_secret_keys: list[str] | None = None
    conversations_path: str | None = None
    bash_events_dir: str | None = None
    conversation_worktree_root: str | None = None
    webhooks: list[AgentServerWebhookSpec] | None = None
    web_url: str | None = None
    allow_cors_origins: list[str] | None = None
    max_concurrent_runs: int | None = Field(default=None, ge=1)
    env: dict[str, str] | None = None
    hindsight_memory: AgentServerHindsightMemory | None = None


class AgentServerInitStatus(BaseModel):
    state: Literal['dormant', 'initializing', 'ready']
    error: str | None = None
