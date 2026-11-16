from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, PositiveInt, model_validator

from openhands.agent_server.utils import utc_now


class SandboxOperatingSystem(str, Enum):
    LINUX = 'linux'
    WINDOWS = 'windows'


class SandboxResourceClass(str, Enum):
    STANDARD = 'standard'
    HIGH_MEMORY = 'high-memory'
    BUILD = 'build'


class SandboxSpecAvailability(str, Enum):
    AVAILABLE = 'available'
    UNAVAILABLE = 'unavailable'
    DISABLED = 'disabled'


class SandboxToleration(BaseModel):
    key: str
    operator: str = 'Equal'
    value: str | None = None
    effect: str | None = None


class SandboxSpecInfo(BaseModel):
    """Administrator-authored execution profile for creating a sandbox."""

    id: str
    display_name: str | None = None
    description: str | None = None
    command: list[str] | None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    source: Literal['bootstrap', 'admin'] = 'admin'
    immutable: bool = False
    bootstrap_revision: str | None = None
    enabled: bool = True
    published: bool = True
    availability: SandboxSpecAvailability = SandboxSpecAvailability.AVAILABLE
    availability_reason: str | None = None
    operating_system: SandboxOperatingSystem = SandboxOperatingSystem.LINUX
    platform: str = 'linux/amd64'
    resource_class: SandboxResourceClass = SandboxResourceClass.STANDARD
    image: str | None = None
    shell: list[str] = Field(default_factory=list)
    durable_state_dir: str | None = None
    template_name: str | None = None
    warm_pool_name: str | None = None
    warm_pool_replicas: int = Field(default=0, ge=0)
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None
    cpu_cores: PositiveInt | None = None
    memory_gib: PositiveInt | None = None
    storage_class: str | None = None
    storage_size: str | None = None
    workspace_storage_class: str | None = None
    workspace_storage_size: str | None = None
    durable_storage_class: str | None = None
    durable_storage_size: str | None = None
    durable_volume_claim_name: str | None = None
    durable_sub_path_template: str | None = None
    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[SandboxToleration] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    max_concurrent_sandboxes: PositiveInt | None = None
    max_concurrent_per_user: PositiveInt | None = None
    initial_env: dict[str, str] = Field(
        default_factory=dict, description='Initial Environment Variables'
    )
    working_dir: str = '/home/openhands/workspace'

    @model_validator(mode='after')
    def validate_execution_profile(self):
        if (
            self.durable_sub_path_template is not None
            and '{sandbox_id}' not in self.durable_sub_path_template
        ):
            raise ValueError('durable_sub_path_template must contain {sandbox_id}')
        if (
            self.operating_system == SandboxOperatingSystem.WINDOWS
            and self.enabled
            and self.published
            and self.availability == SandboxSpecAvailability.AVAILABLE
        ):
            import re

            if not self.image or not re.search(r'@sha256:[0-9a-fA-F]{64}$', self.image):
                raise ValueError(
                    'Available Windows profiles require a digest-pinned image'
                )
            if self.platform != 'windows/amd64':
                raise ValueError(
                    'Available Windows profiles require platform windows/amd64'
                )
        return self


class SandboxSpecInfoPage(BaseModel):
    items: list[SandboxSpecInfo]
    next_page_id: str | None = None


class SandboxSpecCreate(BaseModel):
    profile: SandboxSpecInfo


class SandboxSpecUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    published: bool | None = None
    availability: SandboxSpecAvailability | None = None
    availability_reason: str | None = None
    resource_class: SandboxResourceClass | None = None
    platform: str | None = None
    image: str | None = None
    shell: list[str] | None = None
    durable_state_dir: str | None = None
    template_name: str | None = None
    warm_pool_name: str | None = None
    warm_pool_replicas: int | None = Field(default=None, ge=0)
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None
    cpu_cores: PositiveInt | None = None
    memory_gib: PositiveInt | None = None
    storage_class: str | None = None
    storage_size: str | None = None
    workspace_storage_class: str | None = None
    workspace_storage_size: str | None = None
    durable_storage_class: str | None = None
    durable_storage_size: str | None = None
    durable_volume_claim_name: str | None = None
    durable_sub_path_template: str | None = None
    node_selector: dict[str, str] | None = None
    tolerations: list[SandboxToleration] | None = None
    capabilities: list[str] | None = None
    max_concurrent_sandboxes: PositiveInt | None = None
    max_concurrent_per_user: PositiveInt | None = None
    initial_env: dict[str, str] | None = None
    working_dir: str | None = None
    command: list[str] | None = None


class SandboxSpecAdminCapabilities(BaseModel):
    can_manage: bool
