"""Sandbox specs for the kubernetes (agent-sandbox) backend.

With agent-sandbox the pod is defined cluster side by a ``SandboxTemplate``, and
that template also fixes the agent server image. A spec here therefore names the
``SandboxWarmPool`` that references the template rather than naming an image.

Offering more than one spec lets an operator expose several runtimes, for example a
plain pool and a gVisor-isolated one, for users to choose between.
"""

import json
import os
from pathlib import Path
from typing import AsyncGenerator

from fastapi import Request
from pydantic import Field

from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxOperatingSystem,
    SandboxResourceClass,
    SandboxSpecAvailability,
    SandboxSpecInfo,
    SandboxToleration,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    SandboxSpecServiceInjector,
    get_agent_server_env,
)
from openhands.app_server.sandbox.sql_sandbox_spec_service import (
    SQLSandboxSpecService,
)
from openhands.app_server.services.injector import InjectorState

DEFAULT_WARM_POOL = 'openhands-pool'
WINDOWS_STANDARD_POOL = 'windows-server-2025-amd64'
LINUX_AGENT_SERVER_IMAGE = (
    'ghcr.io/openhands/agent-server:1.37.1-python-p4mcp-2026.4.3126709'
)
WINDOWS_AGENT_SERVER_IMAGE = os.getenv('OH_WINDOWS_AGENT_SERVER_IMAGE')


def _durable_state_pvc() -> str:
    return os.getenv('OH_HALO_DURABLE_STATE_PVC', 'openhands-halo-conversations')


def _durable_storage_class() -> str:
    return os.getenv('OH_HALO_DURABLE_STORAGE_CLASS', 'openhands-halo-azurefile')


def get_default_sandbox_specs() -> list[SandboxSpecInfo]:
    return [
        SandboxSpecInfo(
            id=DEFAULT_WARM_POOL,
            display_name='Linux Standard',
            source='bootstrap',
            immutable=True,
            bootstrap_revision='builtin-linux-v1',
            # The template's container command applies; the claim only injects env.
            command=None,
            initial_env={
                'OH_ENABLE_VNC': '0',
                'LOG_JSON': 'true',
                'OH_CONVERSATIONS_PATH': '/openhands/conversations',
                'OH_BASH_EVENTS_DIR': '/openhands/bash_events',
                'OH_PERSISTENCE_DIR': '/openhands/persistence',
                'OH_CONVERSATION_WORKTREE_ROOT': '/workspace/worktrees',
                'OH_WORKSPACE_PATH': '/workspace/project',
                'PYTHONUNBUFFERED': '1',
                **get_agent_server_env(),
            },
            working_dir='/workspace/project',
            operating_system=SandboxOperatingSystem.LINUX,
            platform='linux/amd64',
            resource_class=SandboxResourceClass.STANDARD,
            image=LINUX_AGENT_SERVER_IMAGE,
            durable_state_dir='/openhands',
            template_name='openhands-agent-server',
            warm_pool_name=DEFAULT_WARM_POOL,
            cpu_request='500m',
            cpu_limit='2',
            memory_request='1Gi',
            memory_limit='4Gi',
            cpu_cores=2,
            memory_gib=4,
            storage_size='5Gi',
            workspace_storage_class='managed-csi',
            workspace_storage_size='5Gi',
            durable_storage_class=_durable_storage_class(),
            durable_volume_claim_name=_durable_state_pvc(),
            durable_sub_path_template='{sandbox_id}',
            capabilities=['git', 'perforce'],
        ),
        SandboxSpecInfo(
            id=WINDOWS_STANDARD_POOL,
            display_name='Windows Server 2025 Standard',
            source='bootstrap',
            immutable=True,
            bootstrap_revision='builtin-windows-2025-v1',
            description='Native Windows Server 2025 container on AKS Windows nodes.',
            command=None,
            initial_env={
                'OH_ENABLE_VNC': '0',
                'LOG_JSON': 'true',
                'OH_CONVERSATIONS_PATH': r'C:\OpenHands\conversations',
                'OH_BASH_EVENTS_DIR': r'C:\OpenHands\bash_events',
                'OH_PERSISTENCE_DIR': r'C:\OpenHands\persistence',
                'OH_CONVERSATION_WORKTREE_ROOT': r'C:\workspace\worktrees',
                'OH_WORKSPACE_PATH': r'C:\workspace\project',
                'PYTHONUNBUFFERED': '1',
                'OH_SANDBOX_OPERATING_SYSTEM': 'windows',
                **get_agent_server_env(),
            },
            working_dir=r'C:\workspace\project',
            operating_system=SandboxOperatingSystem.WINDOWS,
            platform='windows/amd64',
            resource_class=SandboxResourceClass.STANDARD,
            image=WINDOWS_AGENT_SERVER_IMAGE,
            shell=['powershell.exe', '-NoLogo', '-NoProfile'],
            durable_state_dir=r'C:\OpenHands',
            template_name='openhands-agent-server-windows-2025',
            warm_pool_name=WINDOWS_STANDARD_POOL,
            cpu_request='2',
            cpu_limit='4',
            memory_request='4Gi',
            memory_limit='8Gi',
            cpu_cores=4,
            memory_gib=8,
            storage_class='managed-csi',
            storage_size='20Gi',
            workspace_storage_class='managed-csi',
            workspace_storage_size='20Gi',
            durable_storage_class=_durable_storage_class(),
            durable_volume_claim_name=_durable_state_pvc(),
            durable_sub_path_template='{sandbox_id}',
            node_selector={
                'kubernetes.io/os': 'windows',
                'node.kubernetes.io/windows-build': '10.0.26100',
            },
            tolerations=[
                SandboxToleration(
                    key='os',
                    operator='Equal',
                    value='windows',
                    effect='NoSchedule',
                )
            ],
            capabilities=[
                'git',
                'perforce',
                'runtime_image_contract_v1',
                'native_windows_runtime_v1',
            ],
            availability=(
                SandboxSpecAvailability.AVAILABLE
                if WINDOWS_AGENT_SERVER_IMAGE
                else SandboxSpecAvailability.UNAVAILABLE
            ),
            availability_reason=(
                None
                if WINDOWS_AGENT_SERVER_IMAGE
                else (
                    'Set OH_WINDOWS_AGENT_SERVER_IMAGE to a digest-pinned '
                    'ghcr.io/openhands/agent-server:<version-or-sha>-'
                    'windows-ltsc2025-amd64 image.'
                )
            ),
        ),
    ]


def get_bootstrap_sandbox_specs() -> list[SandboxSpecInfo]:
    """Load immutable deployment profiles, with a JSON file overriding built-ins.

    ``OH_SANDBOX_SPEC_BOOTSTRAP_FILE`` contains a JSON array of SandboxSpecInfo
    objects. Entries replace built-ins with the same stable id and are stamped as
    bootstrap-managed/immutable regardless of file contents.
    """
    profiles = {profile.id: profile for profile in get_default_sandbox_specs()}
    bootstrap_file = os.getenv('OH_SANDBOX_SPEC_BOOTSTRAP_FILE')
    if not bootstrap_file:
        return list(profiles.values())
    raw = json.loads(Path(bootstrap_file).read_text(encoding='utf-8'))
    if not isinstance(raw, list):
        raise ValueError('OH_SANDBOX_SPEC_BOOTSTRAP_FILE must contain a JSON array')
    for item in raw:
        profile = SandboxSpecInfo.model_validate(item).model_copy(
            update={'source': 'bootstrap', 'immutable': True}
        )
        profiles[profile.id] = profile
    return list(profiles.values())


class KubernetesSandboxSpecServiceInjector(SandboxSpecServiceInjector):
    specs: list[SandboxSpecInfo] = Field(
        default_factory=get_bootstrap_sandbox_specs,
        description=(
            'Preset list of sandbox specs. Each id names a SandboxWarmPool in the '
            'configured namespace.'
        ),
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxSpecService, None]:
        from openhands.app_server.config import get_db_session

        async with get_db_session(state, request) as db_session:
            service = SQLSandboxSpecService(db_session=db_session)
            await service.seed_sandbox_specs(self.specs)
            yield service
