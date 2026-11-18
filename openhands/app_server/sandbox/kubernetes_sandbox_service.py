"""Sandbox service backed by kubernetes-sigs/agent-sandbox.

Each sandbox is a ``SandboxClaim``; the agent-sandbox controller turns that into a
``Sandbox`` and a pod running the agent server. Compared with the docker service
this gains multi-node scheduling, pause/resume that keeps a persistent volume, and
optional gVisor or Kata isolation. All of that is configured on the cluster side in
a ``SandboxTemplate`` rather than here.

Like the docker service this holds no state of its own: the cluster is the source
of truth. The per-sandbox session API key is injected as an environment variable on
the claim and read back from it, mirroring how the docker service uses container
environment variables.

Requires a ``SandboxWarmPool`` (and the ``SandboxTemplate`` it references) to exist
in the target namespace. The template must allow environment injection
(``envVarsInjectionPolicy: Allowed`` or ``Overrides``) so the session key reaches
the agent server.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, Mapping

import base62
from fastapi import Request
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, ConfigDict, Field

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.sandbox_access import SandboxAccessService
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    VSCODE,
    ExposedUrl,
    SandboxInfo,
    SandboxPage,
    SandboxRecord,
    SandboxStartupPhase,
    SandboxStartupProgress,
    SandboxStartupProgressStatus,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import (
    SESSION_API_KEY_VARIABLE,
    WEBHOOK_CALLBACK_VARIABLE,
    SandboxService,
    SandboxServiceInjector,
    SandboxStartupObservation,
)
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxOperatingSystem,
    SandboxSpecInfo,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    resolve_sandbox_spec,
)
from openhands.app_server.services.injector import InjectorState

_logger = logging.getLogger(__name__)

# agent-sandbox custom resources.
CLAIM_GROUP = 'extensions.agents.x-k8s.io'
CLAIM_VERSION = 'v1beta1'
CLAIM_PLURAL = 'sandboxclaims'
WARM_POOL_PLURAL = 'sandboxwarmpools'
TEMPLATE_PLURAL = 'sandboxtemplates'
SANDBOX_GROUP = 'agents.x-k8s.io'
SANDBOX_VERSION = 'v1beta1'
SANDBOX_PLURAL = 'sandboxes'

# Labels used to find the claims this service owns. Label values allow upper case,
# so the sandbox id can be stored verbatim; resource names cannot, hence the
# separate sanitised claim name.
MANAGED_BY_LABEL = 'app.kubernetes.io/managed-by'
MANAGED_BY_VALUE = 'openhands-app-server'
SANDBOX_ID_LABEL = 'agents.openhands.dev/sandbox-id'
# The spec id is an image name, which is not a valid label value.
SPEC_ID_ANNOTATION = 'agents.openhands.dev/sandbox-spec-id'
TEMPLATE_NAME_ANNOTATION = 'agents.openhands.dev/sandbox-template-name'
WORKSPACE_STORAGE_CLASS_ANNOTATION = 'agents.openhands.dev/workspace-storage-class'
WORKSPACE_STORAGE_SIZE_ANNOTATION = 'agents.openhands.dev/workspace-storage-size'
DURABLE_PVC_ANNOTATION = 'agents.openhands.dev/durable-pvc'
DURABLE_STORAGE_CLASS_ANNOTATION = 'agents.openhands.dev/durable-storage-class'
DURABLE_SUB_PATH_ANNOTATION = 'agents.openhands.dev/durable-sub-path'
# Lookup index for the session key. The env var stays the canonical secret; this is
# a one-way hash so the API server can filter instead of us scanning every claim.
SESSION_KEY_HASH_LABEL = 'agents.openhands.dev/session-key-hash'
SECRET_KEY_VARIABLE = 'OH_SECRET_KEY'

_INVALID_NAME_CHARS = re.compile(r'[^a-z0-9-]+')
# Sandbox ids travel as label values, so they are limited to the label charset and
# length; anything else would either be rejected or corrupt a label selector.
_VALID_SANDBOX_ID = re.compile(r'[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$')
_MAX_NAME_LENGTH = 63
_NAME_DIGEST_LENGTH = 6
# Ready=False reasons the controller treats as terminal: the pod finished either
# way, or the claim outlived its lifecycle.
_TERMINAL_READY_REASONS = frozenset({'PodFailed', 'PodSucceeded', 'SandboxExpired'})
_TERMINAL_CONTAINER_REASONS = frozenset(
    {
        'CreateContainerConfigError',
        'CreateContainerError',
        'ErrImagePull',
        'ImagePullBackOff',
        'InvalidImageName',
        'RunContainerError',
    }
)
_SAFE_CONTAINER_REASON = {
    'CreateContainerConfigError': 'CONTAINER_CONFIG_ERROR',
    'CreateContainerError': 'CONTAINER_CREATE_ERROR',
    'ErrImagePull': 'IMAGE_PULL_ERROR',
    'ImagePullBackOff': 'IMAGE_PULL_BACKOFF',
    'InvalidImageName': 'INVALID_IMAGE_NAME',
    'RunContainerError': 'CONTAINER_RUNTIME_ERROR',
}
_SAFE_STORAGE_REASON = {
    'FailedAttachVolume': 'STORAGE_ATTACH_FAILED',
    'FailedMount': 'STORAGE_MOUNT_FAILED',
    'ProvisioningFailed': 'STORAGE_PROVISIONING_FAILED',
}
_TERMINAL_STORAGE_EVENT_REASONS = frozenset(
    {'FailedAttachVolume', 'FailedMount', 'ProvisioningFailed'}
)
_TRANSIENT_STORAGE_PROVISIONING_MARKERS = ('no topology key found for node',)
_CAPACITY_EVENT_REASONS = frozenset(
    {'FailedScheduling', 'NotTriggerScaleUp', 'TriggeredScaleUp'}
)


def _serialize_kubernetes_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, 'to_dict'):
        result = value.to_dict()
        return result if isinstance(result, dict) else {}
    result = k8s_client.ApiClient().sanitize_for_serialization(value)
    return result if isinstance(result, dict) else {}


def _value(mapping: dict[str, Any], *names: str, default=None):
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _owner_names(resource: dict[str, Any]) -> set[str]:
    metadata = resource.get('metadata') or {}
    owners = _value(metadata, 'ownerReferences', 'owner_references', default=[]) or []
    return {
        str(_value(owner, 'name'))
        for owner in owners
        if isinstance(owner, dict) and _value(owner, 'name')
    }


def _resource_name(resource: dict[str, Any]) -> str | None:
    metadata = resource.get('metadata') or {}
    name = _value(metadata, 'name')
    return str(name) if name else None


def _event_sort_key(event: dict[str, Any]) -> str:
    metadata = event.get('metadata') or {}
    return str(
        _value(
            event,
            'eventTime',
            'event_time',
            'lastTimestamp',
            'last_timestamp',
            default=_value(
                metadata, 'creationTimestamp', 'creation_timestamp', default=''
            ),
        )
        or ''
    )


def _condition_progress_state(resource: dict[str, Any]) -> list[dict[str, Any]]:
    conditions = (resource.get('status') or {}).get('conditions') or []
    return [
        {
            'type': _value(condition, 'type'),
            'status': _value(condition, 'status'),
            'reason': _value(condition, 'reason'),
            'last_transition_time': _value(
                condition,
                'lastTransitionTime',
                'last_transition_time',
            ),
        }
        for condition in conditions
        if isinstance(condition, dict)
    ]


class ExposedPort(BaseModel):
    """Port within the sandbox pod that should be surfaced as a URL."""

    name: str
    description: str
    container_port: int = 8000

    model_config = ConfigDict(frozen=True)


def _default_exposed_ports() -> list[ExposedPort]:
    ports = [
        ExposedPort(
            name=AGENT_SERVER,
            description='The port on which the agent server runs within the pod',
            container_port=8000,
        ),
        ExposedPort(
            name=VSCODE,
            description='The port on which the VSCode server runs within the pod',
            container_port=8001,
        ),
    ]
    if os.getenv('OH_ENABLE_VSCODE', '0').lower() not in ('1', 'true'):
        ports = [port for port in ports if port.name != VSCODE]
    return ports


@dataclass
class KubernetesSandboxService(SandboxService):
    """Sandbox service built on kubernetes-sigs/agent-sandbox.

    The kubernetes client is synchronous, so calls are dispatched to a worker
    thread to avoid blocking the event loop.
    """

    sandbox_spec_service: SandboxSpecService
    namespace: str
    warm_pool: str | None
    claim_name_prefix: str
    sandbox_url_pattern: str
    webhook_base_url: str
    exposed_ports: list[ExposedPort]
    max_num_sandboxes: int
    web_url: str | None = None
    permitted_cors_origins: list[str] = field(default_factory=list)
    inject_session_key: bool = True
    shutdown_after_seconds: int | None = None
    default_sandbox_spec_id: str | None = None
    validate_profile_infrastructure: bool = True
    access_service: SandboxAccessService | None = None
    _custom_objects: Any = None
    _core_v1: Any = None

    def get_control_plane_base_url(self) -> str | None:
        return self.webhook_base_url.rstrip('/')

    def __post_init__(self) -> None:
        if self._custom_objects is None:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
            self._custom_objects = k8s_client.CustomObjectsApi()
            self._core_v1 = k8s_client.CoreV1Api()

    # ── Naming ────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_sandbox_id(sandbox_id: str) -> None:
        """Reject ids that cannot be stored as a label value.

        The id is the lookup key, so it has to survive a label selector intact:
        characters such as ``,`` and ``=`` would otherwise change the meaning of
        the selector rather than just fail to match.
        """
        if not _VALID_SANDBOX_ID.match(sandbox_id):
            raise SandboxError(
                f'Invalid sandbox id {sandbox_id!r}: must be 1 to 63 characters of '
                'letters, digits, dot, dash or underscore, starting and ending '
                'with a letter or digit'
            )

    def _claim_name(self, sandbox_id: str) -> str:
        """Build an RFC 1123 resource name for a sandbox id.

        Sanitising alone is not injective (``a_b`` and ``a.b`` both become ``a-b``),
        so a short digest of the original id is appended to keep distinct sandboxes
        on distinct claims. On a cold start this name is also the Sandbox and
        Service name, so it has to stay within the 63 character DNS label limit.
        """
        digest = hashlib.sha256(sandbox_id.encode()).hexdigest()[:_NAME_DIGEST_LENGTH]
        suffix = f'-{digest}'
        budget = _MAX_NAME_LENGTH - len(self.claim_name_prefix) - len(suffix)
        name = _INVALID_NAME_CHARS.sub('-', sandbox_id.lower()).strip('-')
        name = name[:budget].strip('-') if budget > 0 else ''
        if not name:
            prefix = self.claim_name_prefix[: _MAX_NAME_LENGTH - len(digest)]
            return f'{prefix}{digest}'
        return f'{self.claim_name_prefix}{name}{suffix}'

    @staticmethod
    def _session_key_hash(session_api_key: str) -> str:
        """One-way index for a session key, short enough for a label value."""
        return hashlib.sha256(session_api_key.encode()).hexdigest()[:32]

    # ── Kubernetes access (sync, run in a worker thread) ───────────────────────

    def _list_claims_sync(self, extra_selector: str | None = None) -> list[dict]:
        selector = f'{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}'
        if extra_selector:
            selector = f'{selector},{extra_selector}'
        response = self._custom_objects.list_namespaced_custom_object(
            group=CLAIM_GROUP,
            version=CLAIM_VERSION,
            namespace=self.namespace,
            plural=CLAIM_PLURAL,
            label_selector=selector,
        )
        return response.get('items', [])

    def _get_claim_sync(self, sandbox_id: str) -> dict | None:
        # Characters such as ',' and '=' would change the meaning of the selector,
        # so an unusable id is treated as not found rather than interpolated.
        if not _VALID_SANDBOX_ID.match(sandbox_id):
            return None
        response = self._custom_objects.list_namespaced_custom_object(
            group=CLAIM_GROUP,
            version=CLAIM_VERSION,
            namespace=self.namespace,
            plural=CLAIM_PLURAL,
            label_selector=(
                f'{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{SANDBOX_ID_LABEL}={sandbox_id}'
            ),
        )
        items = response.get('items', [])
        return items[0] if items else None

    def _get_sandbox_object_sync(self, sandbox_name: str) -> dict | None:
        try:
            return self._custom_objects.get_namespaced_custom_object(
                group=SANDBOX_GROUP,
                version=SANDBOX_VERSION,
                namespace=self.namespace,
                plural=SANDBOX_PLURAL,
                name=sandbox_name,
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _get_startup_resources_sync(
        self, claim: dict[str, Any], sandbox: dict[str, Any] | None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if self._core_v1 is None:
            return [], [], []
        claim_name = _resource_name(claim)
        sandbox_name = _resource_name(sandbox or {}) or _sandbox_name(claim)
        owner_names = {name for name in (claim_name, sandbox_name) if name}
        durable_pvc_name = ((claim.get('metadata') or {}).get('annotations') or {}).get(
            DURABLE_PVC_ANNOTATION
        )

        pods_response = self._core_v1.list_namespaced_pod(namespace=self.namespace)
        pods = [
            _serialize_kubernetes_object(item)
            for item in getattr(pods_response, 'items', [])
        ]
        pods = [
            pod
            for pod in pods
            if _owner_names(pod) & owner_names
            or any(
                value in owner_names
                for value in ((pod.get('metadata') or {}).get('labels') or {}).values()
            )
        ]

        pvc_response = self._core_v1.list_namespaced_persistent_volume_claim(
            namespace=self.namespace
        )
        pvcs = [
            _serialize_kubernetes_object(item)
            for item in getattr(pvc_response, 'items', [])
        ]
        pvcs = [
            pvc
            for pvc in pvcs
            if _owner_names(pvc) & owner_names
            or _resource_name(pvc) == durable_pvc_name
        ]

        event_names = owner_names | {
            name
            for resource in [*pods, *pvcs]
            if (name := _resource_name(resource)) is not None
        }
        events_by_uid: dict[str, dict[str, Any]] = {}
        for resource_name in event_names:
            response = self._core_v1.list_namespaced_event(
                namespace=self.namespace,
                field_selector=f'involvedObject.name={resource_name}',
            )
            for item in getattr(response, 'items', []):
                event = _serialize_kubernetes_object(item)
                metadata = event.get('metadata') or {}
                key = str(
                    _value(metadata, 'uid', default=_resource_name(event) or id(item))
                )
                events_by_uid[key] = event
        events = sorted(events_by_uid.values(), key=_event_sort_key)
        return pods, pvcs, events

    def _get_extension_object_sync(self, plural: str, name: str) -> dict:
        try:
            return self._custom_objects.get_namespaced_custom_object(
                group=CLAIM_GROUP,
                version=CLAIM_VERSION,
                namespace=self.namespace,
                plural=plural,
                name=name,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise SandboxError(
                    f'Kubernetes {plural} resource {name!r} was not found'
                ) from exc
            raise

    def _validate_profile_infrastructure_sync(
        self, sandbox_spec: SandboxSpecInfo, warm_pool: str
    ) -> None:
        pool = self._get_extension_object_sync(WARM_POOL_PLURAL, warm_pool)
        template_name = ((pool.get('spec') or {}).get('sandboxTemplateRef') or {}).get(
            'name'
        )
        if (
            not isinstance(template_name, str)
            or template_name != sandbox_spec.template_name
        ):
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} expects template '
                f'{sandbox_spec.template_name!r}, but warm pool {warm_pool!r} '
                f'references {template_name!r}'
            )

        template = self._get_extension_object_sync(TEMPLATE_PLURAL, template_name)
        template_spec = template.get('spec') or {}
        if template_spec.get('volumeClaimTemplatesPolicy') != 'Overrides':
            raise SandboxError(
                f'SandboxTemplate {template_name!r} must allow workspace volume '
                'claim overrides'
            )

        pod_spec = (template_spec.get('podTemplate') or {}).get('spec') or {}
        containers = pod_spec.get('containers') or []
        container = next(
            (item for item in containers if item.get('name') == 'agent-server'),
            containers[0] if containers else None,
        )
        if container is None:
            raise SandboxError(
                f'SandboxTemplate {template_name!r} has no agent-server container'
            )
        if sandbox_spec.image and container.get('image') != sandbox_spec.image:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} image does not match '
                f'SandboxTemplate {template_name!r}'
            )
        if container.get('workingDir') != sandbox_spec.working_dir:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} working directory does '
                f'not match SandboxTemplate {template_name!r}'
            )

        volumes = {
            volume.get('name'): volume for volume in pod_spec.get('volumes') or []
        }
        durable_volume = volumes.get('openhands-state') or {}
        durable_claim_name = (durable_volume.get('persistentVolumeClaim') or {}).get(
            'claimName'
        )
        if durable_claim_name != sandbox_spec.durable_volume_claim_name:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} durable PVC does not '
                f'match SandboxTemplate {template_name!r}'
            )
        if self._core_v1 is None:
            raise SandboxError(
                'Kubernetes core API client is required to validate durable storage'
            )
        try:
            durable_pvc = self._core_v1.read_namespaced_persistent_volume_claim(
                name=sandbox_spec.durable_volume_claim_name,
                namespace=self.namespace,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise SandboxError(
                    f'Durable PVC {sandbox_spec.durable_volume_claim_name!r} '
                    'was not found'
                ) from exc
            raise
        durable_storage_class = getattr(
            getattr(durable_pvc, 'spec', None), 'storage_class_name', None
        )
        if durable_storage_class != sandbox_spec.durable_storage_class:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} durable storage class '
                f'does not match PVC {sandbox_spec.durable_volume_claim_name!r}'
            )

        mounts = {
            mount.get('name'): mount for mount in container.get('volumeMounts') or []
        }
        workspace_mount = mounts.get('workspace') or {}
        durable_mount = mounts.get('openhands-state') or {}
        if workspace_mount.get('mountPath') != sandbox_spec.workspace_mount_path:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} workspace mount does not '
                f'match SandboxTemplate {template_name!r}'
            )
        if durable_mount.get('mountPath') != sandbox_spec.durable_mount_path:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} durable mount does not '
                f'match SandboxTemplate {template_name!r}'
            )
        if sandbox_spec.operating_system == SandboxOperatingSystem.WINDOWS:
            if 'subPath' in durable_mount or 'subPathExpr' in durable_mount:
                raise SandboxError(
                    f'Windows SandboxTemplate {template_name!r} cannot use volume '
                    'subPath mounts'
                )
        elif durable_mount.get('subPathExpr') != '$(OH_SANDBOX_ID)':
            raise SandboxError(
                f'Linux SandboxTemplate {template_name!r} must isolate durable '
                'state by OH_SANDBOX_ID'
            )

        if (
            sandbox_spec.node_selector
            and (pod_spec.get('nodeSelector') or {}) != sandbox_spec.node_selector
        ):
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} node selector does not '
                f'match SandboxTemplate {template_name!r}'
            )
        expected_tolerations = [
            toleration.model_dump(exclude_none=True)
            for toleration in sandbox_spec.tolerations
        ]
        if (
            expected_tolerations
            and (pod_spec.get('tolerations') or []) != expected_tolerations
        ):
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} tolerations do not match '
                f'SandboxTemplate {template_name!r}'
            )

        resources = container.get('resources') or {}
        requests = resources.get('requests') or {}
        limits = resources.get('limits') or {}
        expected_resources = {
            'cpu_request': (requests.get('cpu'), sandbox_spec.cpu_request),
            'memory_request': (
                requests.get('memory'),
                sandbox_spec.memory_request,
            ),
            'cpu_limit': (limits.get('cpu'), sandbox_spec.cpu_limit),
            'memory_limit': (limits.get('memory'), sandbox_spec.memory_limit),
        }
        mismatched_resources = [
            name
            for name, (actual, expected) in expected_resources.items()
            if expected is not None and str(actual) != expected
        ]
        if mismatched_resources:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} resources '
                f'{mismatched_resources!r} do not match SandboxTemplate '
                f'{template_name!r}'
            )

    def _create_claim_sync(self, body: dict) -> dict:
        return self._custom_objects.create_namespaced_custom_object(
            group=CLAIM_GROUP,
            version=CLAIM_VERSION,
            namespace=self.namespace,
            plural=CLAIM_PLURAL,
            body=body,
        )

    def _delete_claim_sync(self, name: str) -> None:
        try:
            self._custom_objects.delete_namespaced_custom_object(
                group=CLAIM_GROUP,
                version=CLAIM_VERSION,
                namespace=self.namespace,
                plural=CLAIM_PLURAL,
                name=name,
            )
        except ApiException as e:
            if e.status != 404:
                raise

    def _set_operating_mode_sync(self, sandbox_name: str, mode: str) -> None:
        self._custom_objects.patch_namespaced_custom_object(
            group=SANDBOX_GROUP,
            version=SANDBOX_VERSION,
            namespace=self.namespace,
            plural=SANDBOX_PLURAL,
            name=sandbox_name,
            body={'spec': {'operatingMode': mode}},
        )

    # ── Conversion ────────────────────────────────────────────────────────────

    def _status_from(
        self, claim: dict, sandbox: dict | None
    ) -> tuple[SandboxStatus, str | None]:
        """Derive the sandbox status and any detail from the custom resources."""
        if sandbox is None:
            # The controller has not created (or has removed) the Sandbox yet.
            return SandboxStatus.STARTING, _claim_condition_message(claim)

        if (sandbox.get('spec') or {}).get('operatingMode') == 'Suspended':
            return SandboxStatus.PAUSED, None

        conditions = (sandbox.get('status') or {}).get('conditions') or []
        for condition in conditions:
            if condition.get('type') != 'Ready':
                continue
            if condition.get('status') == 'True':
                return SandboxStatus.RUNNING, None
            reason = condition.get('reason') or ''
            message = condition.get('message')
            if reason in _TERMINAL_READY_REASONS:
                return SandboxStatus.ERROR, message or reason
            return SandboxStatus.STARTING, message

        return SandboxStatus.STARTING, None

    def _startup_progress_from(
        self,
        claim: dict[str, Any],
        sandbox: dict[str, Any] | None,
        pods: list[dict[str, Any]],
        pvcs: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> SandboxStartupObservation:
        now = utc_now()
        sandbox_status, sandbox_detail = self._status_from(claim, sandbox)
        pod = pods[-1] if pods else {}
        pod_status = pod.get('status') or {}
        pod_spec = pod.get('spec') or {}
        pod_phase = str(_value(pod_status, 'phase', default='') or '')
        node_name = _value(pod_spec, 'nodeName', 'node_name')
        container_statuses = (
            _value(
                pod_status,
                'containerStatuses',
                'container_statuses',
                default=[],
            )
            or []
        )
        waiting_reasons: list[str] = []
        running_started_at: list[str] = []
        for container_status in container_statuses:
            if not isinstance(container_status, dict):
                continue
            state = container_status.get('state') or {}
            waiting = state.get('waiting') or {}
            running = state.get('running') or {}
            reason = _value(waiting, 'reason')
            if reason:
                waiting_reasons.append(str(reason))
            started_at = _value(running, 'startedAt', 'started_at')
            if started_at:
                running_started_at.append(str(started_at))

        pvc_states = {
            _resource_name(pvc) or '': str(
                _value(pvc.get('status') or {}, 'phase', default='') or ''
            )
            for pvc in pvcs
        }
        pvc_phases = list(pvc_states.values())
        durable_pvc_name = ((claim.get('metadata') or {}).get('annotations') or {}).get(
            DURABLE_PVC_ANNOTATION
        )
        event_reasons = [
            str(_value(event, 'reason', default='') or '') for event in events
        ]
        latest_event = events[-1] if events else {}
        latest_event_reason = str(_value(latest_event, 'reason', default='') or '')
        latest_event_message = str(_value(latest_event, 'message', default='') or '')

        phase = SandboxStartupPhase.SCHEDULING
        status = SandboxStartupProgressStatus.PROGRESSING
        reason = 'CLAIM_ACCEPTED'
        message = 'The sandbox claim was accepted and is being scheduled.'
        failure_detail: str | None = None

        terminal_container_reason = next(
            (item for item in waiting_reasons if item in _TERMINAL_CONTAINER_REASONS),
            None,
        )
        storage_event_reasons = _TERMINAL_STORAGE_EVENT_REASONS | {
            'ProvisioningSucceeded',
            'SuccessfulAttachVolume',
            'SuccessfulMountVolume',
        }
        latest_storage_event = next(
            (
                event
                for event in reversed(events)
                if str(_value(event, 'reason', default='') or '')
                in storage_event_reasons
            ),
            {},
        )
        latest_storage_event_reason = str(
            _value(latest_storage_event, 'reason', default='') or ''
        )
        latest_storage_event_message = str(
            _value(latest_storage_event, 'message', default='') or ''
        )
        terminal_storage_reason = (
            latest_storage_event_reason
            if latest_storage_event_reason in _TERMINAL_STORAGE_EVENT_REASONS
            else None
        )
        if terminal_storage_reason == 'ProvisioningFailed' and any(
            marker in latest_storage_event_message.lower()
            for marker in _TRANSIENT_STORAGE_PROVISIONING_MARKERS
        ):
            terminal_storage_reason = None
        latest_scheduling_event = next(
            (
                event
                for event in reversed(events)
                if str(_value(event, 'reason', default='') or '')
                in _CAPACITY_EVENT_REASONS
            ),
            {},
        )
        latest_scheduling_reason = str(
            _value(latest_scheduling_event, 'reason', default='') or ''
        )
        latest_scheduling_message = str(
            _value(latest_scheduling_event, 'message', default='') or ''
        )
        retryable_scheduling_failure = latest_scheduling_reason in {
            'FailedScheduling',
            'NotTriggerScaleUp',
        } and (
            'Insufficient ' in latest_scheduling_message
            or any(
                marker in latest_scheduling_message.lower()
                for marker in (
                    'untolerated taint',
                    'node affinity',
                    'node selector',
                    'volume node affinity conflict',
                )
            )
        )

        if sandbox_status == SandboxStatus.ERROR and not retryable_scheduling_failure:
            phase = SandboxStartupPhase.FAILED
            status = SandboxStartupProgressStatus.FAILED
            reason = 'SANDBOX_TERMINAL'
            message = 'The sandbox controller reported a terminal failure.'
            failure_detail = sandbox_detail
        elif pod_phase == 'Failed':
            phase = SandboxStartupPhase.FAILED
            status = SandboxStartupProgressStatus.FAILED
            reason = 'POD_FAILED'
            message = 'The sandbox pod terminated before becoming ready.'
            failure_detail = latest_event_message or sandbox_detail or 'PodFailed'
        elif terminal_container_reason:
            phase = SandboxStartupPhase.FAILED
            status = SandboxStartupProgressStatus.FAILED
            reason = _SAFE_CONTAINER_REASON[terminal_container_reason]
            message = (
                'The sandbox runtime image or container configuration could not '
                'be started.'
            )
            failure_detail = terminal_container_reason
        elif terminal_storage_reason:
            phase = SandboxStartupPhase.FAILED
            status = SandboxStartupProgressStatus.FAILED
            reason = _SAFE_STORAGE_REASON[terminal_storage_reason]
            message = 'The sandbox storage could not be provisioned or mounted.'
            failure_detail = (
                f'{terminal_storage_reason}: {latest_storage_event_message}'
                if latest_storage_event_message
                else terminal_storage_reason
            )
        elif pod_phase == 'Running' or running_started_at:
            phase = SandboxStartupPhase.WAITING_FOR_AGENT_SERVER
            reason = 'AGENT_SERVER_NOT_READY'
            message = 'Waiting for the Agent Server readiness probe.'
        elif not node_name and latest_scheduling_reason == 'TriggeredScaleUp':
            phase = SandboxStartupPhase.PROVISIONING_NODE
            reason = 'AUTOSCALER_TRIGGERED'
            message = 'The cluster autoscaler is provisioning a node.'
        elif (
            not node_name
            and latest_scheduling_reason in {'FailedScheduling', 'NotTriggerScaleUp'}
            and 'Insufficient ' in latest_scheduling_message
        ):
            phase = SandboxStartupPhase.WAITING_FOR_CAPACITY
            reason = 'CAPACITY_UNAVAILABLE'
            message = 'Waiting for cluster capacity; the autoscaler may add a node.'
        elif any(item == 'Pending' for item in pvc_phases):
            phase = SandboxStartupPhase.PROVISIONING_STORAGE
            if durable_pvc_name and pvc_states.get(durable_pvc_name) == 'Pending':
                reason = 'DURABLE_STORAGE_PENDING'
                message = 'Attaching durable conversation storage.'
            else:
                reason = 'WORKSPACE_STORAGE_PENDING'
                message = 'Provisioning and attaching workspace storage.'
        elif not pod or not node_name:
            phase = SandboxStartupPhase.SCHEDULING
            reason = 'POD_UNSCHEDULED'
            message = 'Scheduling the sandbox onto a node.'
        elif latest_event_reason == 'Pulling':
            phase = SandboxStartupPhase.PULLING_IMAGE
            reason = 'IMAGE_PULLING'
            message = 'Pulling the sandbox runtime image.'
        elif waiting_reasons:
            phase = SandboxStartupPhase.STARTING_CONTAINER
            reason = 'CONTAINER_STARTING'
            message = 'Starting the sandbox container.'
        elif latest_event_reason == 'Pulled':
            phase = SandboxStartupPhase.STARTING_CONTAINER
            reason = 'IMAGE_PULLED'
            message = 'The sandbox image is present; starting the container.'
        elif sandbox_status == SandboxStatus.RUNNING:
            phase = SandboxStartupPhase.READY
            status = SandboxStartupProgressStatus.READY
            reason = 'SANDBOX_READY'
            message = 'The sandbox and Agent Server are ready.'

        if sandbox_status == SandboxStatus.RUNNING:
            phase = SandboxStartupPhase.READY
            status = SandboxStartupProgressStatus.READY
            reason = 'SANDBOX_READY'
            message = 'The sandbox and Agent Server are ready.'

        progress_state = {
            'phase': phase.value,
            'reason': reason,
            'claim_conditions': _condition_progress_state(claim),
            'sandbox_conditions': _condition_progress_state(sandbox or {}),
            'pod_phase': pod_phase,
            'node_name': node_name,
            'waiting_reasons': waiting_reasons,
            'running_started_at': running_started_at,
            'pvc_phases': pvc_phases,
            'event_reasons': list(dict.fromkeys(event_reasons)),
        }
        progress_key = hashlib.sha256(
            json.dumps(progress_state, sort_keys=True, default=str).encode()
        ).hexdigest()
        return SandboxStartupObservation(
            progress=SandboxStartupProgress(
                phase=phase,
                status=status,
                message=message,
                reason=reason,
                started_at=now,
                updated_at=now,
                phase_started_at=now,
                elapsed_seconds=0,
                phase_elapsed_seconds=0,
                inactivity_seconds=0,
            ),
            progress_key=progress_key,
            failure_detail=failure_detail,
        )

    def _exposed_urls(self, sandbox_id: str, sandbox_name: str) -> list[ExposedUrl]:
        """Build the URLs for a running sandbox.

        ``sandbox_name`` is the Sandbox resource, which is what cluster DNS
        resolves; ``sandbox_id`` is the app-level id, useful for ingress routes
        that key off it.
        """
        return [
            ExposedUrl(
                name=port.name,
                url=self.sandbox_url_pattern.format(
                    sandbox_id=sandbox_id,
                    sandbox_name=sandbox_name,
                    namespace=self.namespace,
                    port=port.container_port,
                ),
                port=port.container_port,
            )
            for port in self.exposed_ports
        ]

    def _session_api_key(self, claim: dict) -> str | None:
        for env in (claim.get('spec') or {}).get('env') or []:
            if env.get('name') == SESSION_API_KEY_VARIABLE:
                return env.get('value')
        return None

    def _init_api_key(self, claim: dict) -> str | None:
        for env in (claim.get('spec') or {}).get('env') or []:
            if env.get('name') == SECRET_KEY_VARIABLE:
                return env.get('value')
        return None

    async def _claim_to_sandbox_info(
        self,
        claim: dict,
        owners: Mapping[str, str] | None = None,
    ) -> SandboxInfo | None:
        metadata = claim.get('metadata') or {}
        labels = metadata.get('labels') or {}
        sandbox_id = labels.get(SANDBOX_ID_LABEL)
        if not sandbox_id:
            return None

        sandbox_name = _sandbox_name(claim)
        sandbox = None
        if sandbox_name:
            sandbox = await asyncio.to_thread(
                self._get_sandbox_object_sync, sandbox_name
            )

        status, status_detail = self._status_from(claim, sandbox)

        created_at = utc_now()
        creation_timestamp = metadata.get('creationTimestamp')
        if creation_timestamp:
            try:
                created_at = datetime.fromisoformat(
                    creation_timestamp.replace('Z', '+00:00')
                )
            except ValueError:
                pass

        annotations = metadata.get('annotations') or {}
        sandbox_spec_id = annotations.get(SPEC_ID_ANNOTATION)
        if not isinstance(sandbox_spec_id, str) or not sandbox_spec_id:
            warm_pool_ref = (claim.get('spec') or {}).get('warmPoolRef') or {}
            warm_pool_name = warm_pool_ref.get('name')
            legacy_spec = None
            if isinstance(warm_pool_name, str) and warm_pool_name:
                legacy_spec = await self.sandbox_spec_service.get_sandbox_spec(
                    warm_pool_name, include_unavailable=True
                )
            if legacy_spec is None:
                legacy_spec = await self.sandbox_spec_service.get_default_sandbox_spec()
            sandbox_spec_id = legacy_spec.id
        if owners is not None:
            created_by_user_id = owners.get(sandbox_id)
        elif self.access_service is not None:
            created_by_user_id = await self.access_service.owner_user_id(sandbox_id)
        else:
            created_by_user_id = None
        return SandboxInfo(
            id=sandbox_id,
            created_by_user_id=created_by_user_id,
            sandbox_spec_id=sandbox_spec_id,
            status=status,
            session_api_key=(
                self._session_api_key(claim)
                if status == SandboxStatus.RUNNING
                else None
            ),
            exposed_urls=(
                self._exposed_urls(sandbox_id, sandbox_name or '')
                if status == SandboxStatus.RUNNING
                else []
            ),
            created_at=created_at,
            status_detail=status_detail,
        )

    # ── SandboxService ────────────────────────────────────────────────────────

    async def search_sandboxes(
        self,
        page_id: str | None = None,
        limit: int = 100,
    ) -> SandboxPage:
        claims = await asyncio.to_thread(self._list_claims_sync)
        owners: dict[str, str] = {}
        if self.access_service is not None:
            sandbox_ids = [
                sandbox_id
                for claim in claims
                if (
                    sandbox_id := (
                        (claim.get('metadata') or {}).get('labels') or {}
                    ).get(SANDBOX_ID_LABEL)
                )
            ]
            owners = await self.access_service.owner_user_ids(sandbox_ids)
        # Each conversion reads the claim's Sandbox, so run them concurrently
        # rather than paying one API round-trip per claim in series.
        infos = await asyncio.gather(
            *[self._claim_to_sandbox_info(claim, owners) for claim in claims]
        )
        items = [info for info in infos if info is not None]
        items.sort(key=lambda item: item.created_at)

        # A malformed page_id starts from the beginning rather than failing the
        # request, matching the remote backend.
        try:
            offset = max(int(page_id), 0) if page_id else 0
        except ValueError:
            offset = 0
        page = items[offset : offset + limit]
        next_page_id = str(offset + limit) if len(items) > offset + limit else None
        return SandboxPage(items=page, next_page_id=next_page_id)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            return None
        return await self._claim_to_sandbox_info(claim)

    async def get_sandbox_init_api_key(self, sandbox_id: str) -> str | None:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            return None
        return self._init_api_key(claim)

    async def get_sandbox_internal_url(
        self, sandbox_id: str, service_name: str
    ) -> str | None:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            return None
        sandbox_name = _sandbox_name(claim)
        if not sandbox_name:
            raise SandboxError(
                f'Sandbox controller has not assigned a runtime for {sandbox_id}'
            )
        sandbox = await asyncio.to_thread(self._get_sandbox_object_sync, sandbox_name)
        if sandbox is None:
            raise SandboxError(f'Sandbox runtime not found: {sandbox_id}')
        service_fqdn = _value(
            sandbox.get('status') or {}, 'serviceFQDN', 'service_fqdn'
        )
        if not isinstance(service_fqdn, str) or not service_fqdn:
            raise SandboxError(
                f'Sandbox runtime has no internal service address: {sandbox_id}'
            )
        port = next(
            (
                exposed_port.container_port
                for exposed_port in self.exposed_ports
                if exposed_port.name == service_name
            ),
            None,
        )
        if port is None:
            raise SandboxError(
                f'Sandbox service port is not configured: {service_name}'
            )
        return f'http://{service_fqdn}:{port}'

    async def get_sandbox_startup_observation(
        self, sandbox_id: str
    ) -> SandboxStartupObservation:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            raise SandboxError(f'Sandbox not found: {sandbox_id}')
        sandbox_name = _sandbox_name(claim)
        sandbox = (
            await asyncio.to_thread(self._get_sandbox_object_sync, sandbox_name)
            if sandbox_name
            else None
        )
        pods, pvcs, events = await asyncio.to_thread(
            self._get_startup_resources_sync, claim, sandbox
        )
        return self._startup_progress_from(claim, sandbox, pods, pvcs, events)

    async def _claim_for_session_api_key(self, session_api_key: str) -> dict | None:
        """Find a claim by session key, filtered by the API server.

        The hash label narrows the query to (almost always) a single claim; the
        env var is still compared so a hash collision cannot authenticate.
        """
        selector = f'{SESSION_KEY_HASH_LABEL}={self._session_key_hash(session_api_key)}'
        claims = await asyncio.to_thread(self._list_claims_sync, selector)
        for claim in claims:
            candidate = self._session_api_key(claim)
            if candidate is not None and hmac.compare_digest(
                candidate, session_api_key
            ):
                return claim
        return None

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        claim = await self._claim_for_session_api_key(session_api_key)
        if claim is None:
            return None
        return await self._claim_to_sandbox_info(claim)

    async def get_sandbox_record_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxRecord | None:
        claim = await self._claim_for_session_api_key(session_api_key)
        if claim is None:
            return None
        labels = (claim.get('metadata') or {}).get('labels') or {}
        sandbox_id = labels.get(SANDBOX_ID_LABEL)
        if not sandbox_id:
            return None
        created_by_user_id = None
        if self.access_service is not None:
            created_by_user_id = await self.access_service.owner_user_id(sandbox_id)
        return SandboxRecord(id=sandbox_id, created_by_user_id=created_by_user_id)

    async def start_sandbox(
        self, sandbox_spec_id: str | None = None, sandbox_id: str | None = None
    ) -> SandboxInfo:
        """Create a SandboxClaim for a new sandbox."""
        if sandbox_id is None:
            sandbox_id = base62.encodebytes(os.urandom(16))
        else:
            self._validate_sandbox_id(sandbox_id)
        if self.access_service is not None:
            self.access_service.require_owner()

        # Resolving first keeps an unknown spec id from evicting a sandbox on its
        # way to raising.
        sandbox_spec = await resolve_sandbox_spec(
            sandbox_spec_id,
            self.default_sandbox_spec_id,
            self.sandbox_spec_service,
            _logger,
        )

        await self._reconcile_orphaned_capacity()
        await self._enforce_profile_quota(sandbox_spec)
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1)

        env_vars = {
            name: value.replace('{sandbox_id}', sandbox_id)
            for name, value in sandbox_spec.initial_env.items()
        }
        env_vars['OH_SANDBOX_ID'] = sandbox_id
        env_vars[SECRET_KEY_VARIABLE] = base62.encodebytes(os.urandom(32))
        env_vars[WEBHOOK_CALLBACK_VARIABLE] = (
            f'{self.webhook_base_url.rstrip("/")}/api/v1/webhooks'
        )
        for port in self.exposed_ports:
            env_vars[port.name] = str(port.container_port)

        cors_origins: list[str] = []
        if self.web_url:
            cors_origins.append(self.web_url)
        cors_origins.extend(self.permitted_cors_origins)
        seen: set[str] = set()
        for origin in cors_origins:
            if origin not in seen:
                env_vars[f'OH_ALLOW_CORS_ORIGINS_{len(seen)}'] = origin
                seen.add(origin)

        labels = {
            MANAGED_BY_LABEL: MANAGED_BY_VALUE,
            SANDBOX_ID_LABEL: sandbox_id,
        }
        session_api_key = None
        if self.inject_session_key:
            session_api_key = base62.encodebytes(os.urandom(32))
            env_vars[SESSION_API_KEY_VARIABLE] = session_api_key
            labels[SESSION_KEY_HASH_LABEL] = self._session_key_hash(session_api_key)

        # The warm pool selects the SandboxTemplate (and therefore the image); when
        # it is not pinned in config the spec id names the pool, so the UI can offer
        # more than one runtime.
        spec_warm_pool = getattr(sandbox_spec, 'warm_pool_name', None)
        if not isinstance(spec_warm_pool, str):
            spec_warm_pool = None
        warm_pool = self.warm_pool or spec_warm_pool or sandbox_spec.id

        workspace_storage_class = (
            sandbox_spec.workspace_storage_class or sandbox_spec.storage_class
        )
        workspace_storage_size = (
            sandbox_spec.workspace_storage_size or sandbox_spec.storage_size
        )
        durable_pvc = sandbox_spec.durable_volume_claim_name
        durable_storage_class = sandbox_spec.durable_storage_class
        durable_sub_path_template = sandbox_spec.durable_sub_path_template
        if not workspace_storage_class or not workspace_storage_size:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} must define workspace '
                'storage class and size'
            )
        if (
            not durable_pvc
            or not durable_storage_class
            or not durable_sub_path_template
        ):
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} must define durable PVC, '
                'storage class, and sub-path template'
            )
        durable_sub_path = durable_sub_path_template.replace('{sandbox_id}', sandbox_id)
        template_name = sandbox_spec.template_name or ''
        if self.validate_profile_infrastructure:
            await asyncio.to_thread(
                self._validate_profile_infrastructure_sync,
                sandbox_spec,
                warm_pool,
            )

        created_by_user_id = None
        if self.access_service is not None:
            created_by_user_id = await self.access_service.create_personal_sandbox(
                sandbox_id
            )

        claim: dict[str, Any] = {
            'apiVersion': f'{CLAIM_GROUP}/{CLAIM_VERSION}',
            'kind': 'SandboxClaim',
            'metadata': {
                'name': self._claim_name(sandbox_id),
                'namespace': self.namespace,
                'labels': labels,
                'annotations': {
                    SPEC_ID_ANNOTATION: sandbox_spec.id,
                    TEMPLATE_NAME_ANNOTATION: template_name,
                    WORKSPACE_STORAGE_CLASS_ANNOTATION: workspace_storage_class,
                    WORKSPACE_STORAGE_SIZE_ANNOTATION: workspace_storage_size,
                    DURABLE_PVC_ANNOTATION: durable_pvc,
                    DURABLE_STORAGE_CLASS_ANNOTATION: durable_storage_class,
                    DURABLE_SUB_PATH_ANNOTATION: durable_sub_path,
                },
            },
            'spec': {
                'warmPoolRef': {'name': warm_pool},
                'volumeClaimTemplates': [
                    {
                        'metadata': {'name': 'workspace'},
                        'spec': {
                            'accessModes': ['ReadWriteOnce'],
                            'volumeMode': 'Filesystem',
                            'storageClassName': workspace_storage_class,
                            'resources': {
                                'requests': {'storage': workspace_storage_size}
                            },
                        },
                    }
                ],
                'env': [
                    {'name': name, 'value': value} for name, value in env_vars.items()
                ],
            },
        }
        if self.shutdown_after_seconds is not None:
            shutdown_time = utc_now() + timedelta(seconds=self.shutdown_after_seconds)
            claim['spec']['lifecycle'] = {
                'shutdownTime': shutdown_time.isoformat().replace('+00:00', 'Z'),
                'shutdownPolicy': 'Delete',
            }

        try:
            await asyncio.to_thread(self._create_claim_sync, claim)
        except ApiException as e:
            if e.status == 409:
                raise SandboxError(
                    f'A sandbox with id {sandbox_id} already exists'
                ) from e
            raise SandboxError(f'Could not create sandbox claim: {e.reason}') from e

        return SandboxInfo(
            id=sandbox_id,
            created_by_user_id=created_by_user_id,
            sandbox_spec_id=sandbox_spec.id,
            status=SandboxStatus.STARTING,
            session_api_key=None,
            exposed_urls=[],
        )

    async def _enforce_profile_quota(self, sandbox_spec) -> None:
        max_global = getattr(sandbox_spec, 'max_concurrent_sandboxes', None)
        max_per_user = getattr(sandbox_spec, 'max_concurrent_per_user', None)
        max_global = max_global if isinstance(max_global, int) else None
        max_per_user = max_per_user if isinstance(max_per_user, int) else None
        if max_global is None and max_per_user is None:
            return
        claims = await asyncio.to_thread(self._list_claims_sync)
        matching_ids: list[str] = []
        for claim in claims:
            metadata = claim.get('metadata') or {}
            annotations = metadata.get('annotations') or {}
            labels = metadata.get('labels') or {}
            sandbox_id = labels.get(SANDBOX_ID_LABEL)
            if annotations.get(SPEC_ID_ANNOTATION) == sandbox_spec.id and isinstance(
                sandbox_id, str
            ):
                matching_ids.append(sandbox_id)
        if max_global is not None and len(matching_ids) >= max_global:
            raise SandboxError(
                f'Execution profile {sandbox_spec.id!r} reached its global '
                'concurrency limit'
            )
        if max_per_user is not None and self.access_service is not None:
            owners = await self.access_service.owner_user_ids(matching_ids)
            owned_count = sum(
                owner == self.access_service.user_id for owner in owners.values()
            )
            if owned_count >= max_per_user:
                raise SandboxError(
                    f'Execution profile {sandbox_spec.id!r} reached the current '
                    'user concurrency limit'
                )

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        return await self._set_operating_mode(sandbox_id, 'Running')

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        return await self._set_operating_mode(sandbox_id, 'Suspended')

    async def _set_operating_mode(self, sandbox_id: str, mode: str) -> bool:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            return False
        sandbox_name = _sandbox_name(claim)
        if not sandbox_name:
            # Still being provisioned; there is nothing to suspend or resume yet.
            return mode == 'Running'
        try:
            await asyncio.to_thread(self._set_operating_mode_sync, sandbox_name, mode)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        return True

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            return False
        name = (claim.get('metadata') or {}).get('name')
        if not name:
            return False
        await asyncio.to_thread(self._delete_claim_sync, name)
        if self.access_service is not None:
            await self.access_service.delete_sandbox(sandbox_id)
        return True

    async def reconcile_orphaned_sandboxes(
        self, active_sandbox_ids: set[str]
    ) -> list[str]:
        claims = await asyncio.to_thread(self._list_claims_sync)
        deleted: list[str] = []
        for claim in claims:
            metadata = claim.get('metadata') or {}
            labels = metadata.get('labels') or {}
            sandbox_id = labels.get(SANDBOX_ID_LABEL)
            if (
                labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE
                or not isinstance(sandbox_id, str)
                or sandbox_id in active_sandbox_ids
            ):
                continue
            name = metadata.get('name')
            if not isinstance(name, str) or not name:
                continue
            await asyncio.to_thread(self._delete_claim_sync, name)
            deleted.append(sandbox_id)
        return deleted

    async def _reconcile_orphaned_capacity(self) -> None:
        if self.access_service is None:
            return
        from openhands.app_server.sandbox.sandbox_lifecycle import active_sandbox_ids

        deleted_ids = await self.reconcile_orphaned_sandboxes(
            await active_sandbox_ids(self.access_service.db_session)
        )
        for sandbox_id in deleted_ids:
            await self.access_service.delete_sandbox(sandbox_id)


def _sandbox_name(claim: dict) -> str | None:
    """Name of the Sandbox the controller created for this claim, if any."""
    status = claim.get('status') or {}
    return (status.get('sandbox') or {}).get('name')


def _claim_condition_message(claim: dict) -> str | None:
    """Surface the claim's own condition message while it is still provisioning."""
    for condition in (claim.get('status') or {}).get('conditions') or []:
        if condition.get('status') != 'True':
            return condition.get('message')
    return None


class KubernetesSandboxServiceInjector(SandboxServiceInjector):
    """Dependency injector for kubernetes sandbox services."""

    namespace: str = Field(
        default='default',
        description='Kubernetes namespace holding the warm pool and sandboxes.',
    )
    warm_pool: str | None = Field(
        default=None,
        description=(
            'SandboxWarmPool to claim from. When unset, the sandbox spec id is '
            'used as the pool name, which allows selecting between runtimes.'
        ),
    )
    claim_name_prefix: str = Field(
        default='oh-agent-server-',
        max_length=_MAX_NAME_LENGTH - _NAME_DIGEST_LENGTH,
        description=(
            'Prefix for claim names. Bounded so a claim name still fits the 63 '
            'character DNS label limit once the uniqueness digest is appended.'
        ),
    )
    sandbox_url_pattern: str = Field(
        default='http://{sandbox_name}.{namespace}.svc.cluster.local:{port}',
        description=(
            'URL pattern for reaching a sandbox. Placeholders: {sandbox_name} '
            '(the Sandbox resource, which cluster DNS resolves), {sandbox_id} '
            '(the app-level id), {namespace} and {port}. The default suits an app '
            'server running in the same cluster; browsers need an externally '
            'routable pattern such as https://{sandbox_name}.sandboxes.example.com.'
        ),
    )
    webhook_base_url: str = Field(
        default='http://openhands-app-server.default.svc.cluster.local:3000',
        description=(
            'Base URL of this app server as reachable from a sandbox pod, used '
            'for webhook callbacks.'
        ),
    )
    max_num_sandboxes: int = Field(
        default=10,
        description='Maximum number of sandboxes allowed to run simultaneously',
    )
    inject_session_key: bool = Field(
        default=True,
        description=(
            'Inject a unique session API key into each sandbox. This is required '
            'whenever sandboxes are reachable by more than one user. Turning it '
            'off does not restore warm-pool starts: every claim carries webhook '
            'and port environment variables, and any claim with spec.env is cold '
            'started from the template by design.'
        ),
    )
    shutdown_after_seconds: int | None = Field(
        default=None,
        description=(
            'Optional TTL after which the controller deletes the claim; a safety '
            'net against leaked sandboxes.'
        ),
    )
    exposed_ports: list[ExposedPort] = Field(default_factory=_default_exposed_ports)
    validate_profile_infrastructure: bool = Field(
        default=True,
        description=(
            'Validate the selected warm pool and SandboxTemplate against the '
            'execution profile before creating a claim.'
        ),
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        # Defined inline to prevent circular lookup
        from openhands.app_server.config import (
            get_db_session,
            get_global_config,
            get_sandbox_spec_service,
            get_user_context,
        )

        config = get_global_config()

        async with (
            get_sandbox_spec_service(state) as sandbox_spec_service,
            get_user_context(state, request) as user_context,
            get_db_session(state, request) as db_session,
        ):
            user_id = await user_context.get_user_id()
            yield KubernetesSandboxService(
                sandbox_spec_service=sandbox_spec_service,
                namespace=self.namespace,
                warm_pool=self.warm_pool,
                claim_name_prefix=self.claim_name_prefix,
                sandbox_url_pattern=self.sandbox_url_pattern,
                webhook_base_url=self.webhook_base_url,
                exposed_ports=self.exposed_ports,
                max_num_sandboxes=self.max_num_sandboxes,
                web_url=config.web_url,
                permitted_cors_origins=config.permitted_cors_origins,
                inject_session_key=self.inject_session_key,
                shutdown_after_seconds=self.shutdown_after_seconds,
                default_sandbox_spec_id=(
                    await user_context.get_default_sandbox_spec_id()
                ),
                validate_profile_infrastructure=(self.validate_profile_infrastructure),
                access_service=SandboxAccessService(
                    db_session=db_session,
                    user_id=user_id,
                ),
            )
