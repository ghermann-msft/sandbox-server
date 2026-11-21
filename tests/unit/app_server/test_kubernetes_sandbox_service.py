"""Tests for KubernetesSandboxService.

Covers the mapping between agent-sandbox custom resources and SandboxInfo:
- warm-safe claim creation and out-of-band credential storage
- status derivation from the Sandbox conditions and operating mode
- pause / resume through spec.operatingMode
- lookup by id and by session API key
- delete
No cluster is required: the CustomObjectsApi is mocked.
"""

import base64
import contextlib
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import yaml
from kubernetes.client.rest import ApiException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversationStartRequest,
    AppConversationStartTask,
    AppConversationStartTaskStatus,
)
from openhands.app_server.app_conversation.sql_app_conversation_info_service import (
    StoredConversationMetadata,
)
from openhands.app_server.app_conversation.sql_app_conversation_start_task_service import (
    SQLAppConversationStartTaskService,
    StoredAppConversationStartTask,
)
from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.kubernetes_sandbox_service import (
    CREDENTIALS_SECRET_ANNOTATION,
    DURABLE_PVC_ANNOTATION,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    SANDBOX_ID_LABEL,
    SANDBOX_SECRET_KEY,
    SESSION_API_KEY_SECRET_KEY,
    SESSION_KEY_HASH_LABEL,
    SPEC_ID_ANNOTATION,
    TEMPLATE_NAME_ANNOTATION,
    KubernetesSandboxService,
    KubernetesSandboxServiceInjector,
)
from openhands.app_server.sandbox.kubernetes_sandbox_spec_service import (
    DEFAULT_WARM_POOL,
    WINDOWS_STANDARD_POOL,
    get_bootstrap_sandbox_specs,
    get_default_sandbox_specs,
)
from openhands.app_server.sandbox.sandbox_access import (
    SandboxAccessService,
    StoredSandboxAccess,
)
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    SandboxStartupPhase,
    SandboxStartupProgressStatus,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import (
    SESSION_API_KEY_VARIABLE,
)
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxOperatingSystem,
    SandboxResourceClass,
    SandboxSpecInfo,
)


@pytest.fixture
def mock_sandbox_spec_service():
    service = AsyncMock()
    spec = MagicMock()
    spec.id = 'openhands-pool'
    spec.initial_env = {'LOG_JSON': 'true'}
    spec.command = None
    spec.working_dir = '/workspace/project'
    spec.template_name = 'openhands-agent-server'
    spec.warm_pool_name = 'openhands-pool'
    spec.storage_class = 'managed-csi'
    spec.storage_size = '5Gi'
    spec.workspace_storage_class = 'managed-csi'
    spec.workspace_storage_size = '5Gi'
    spec.durable_storage_class = 'openhands-halo-azurefile'
    spec.durable_volume_claim_name = 'openhands-halo-conversations'
    spec.durable_sub_path_template = '{sandbox_id}'
    service.get_default_sandbox_spec.return_value = spec
    service.get_sandbox_spec.return_value = spec
    return service


@pytest.fixture
def custom_objects():
    return MagicMock()


@pytest.fixture
def core_v1():
    client = MagicMock()
    durable_pvc = MagicMock()
    durable_pvc.spec.storage_class_name = 'openhands-halo-azurefile'
    client.read_namespaced_persistent_volume_claim.return_value = durable_pvc
    return client


@pytest.fixture
def service(mock_sandbox_spec_service, custom_objects, core_v1):
    return KubernetesSandboxService(
        sandbox_spec_service=mock_sandbox_spec_service,
        namespace='agents',
        warm_pool=None,
        claim_name_prefix='oh-agent-server-',
        sandbox_url_pattern='https://{sandbox_name}.example.com:{port}',
        webhook_base_url='http://app-server:3000',
        exposed_ports=[],
        max_num_sandboxes=5,
        agent_server_secret_key='bootstrap-key',
        validate_profile_infrastructure=False,
        _custom_objects=custom_objects,
        _core_v1=core_v1,
    )


def _claim(sandbox_id='sb1', sandbox_name='sb-1', env=None):
    return {
        'metadata': {
            'name': f'oh-agent-server-{sandbox_id}',
            'labels': {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                SANDBOX_ID_LABEL: sandbox_id,
            },
            'annotations': {SPEC_ID_ANNOTATION: 'openhands-pool'},
            'creationTimestamp': '2026-08-19T10:00:00Z',
        },
        'spec': {'env': env if env is not None else []},
        # The controller reports the Sandbox it created under status.sandbox.
        'status': {'sandbox': {'name': sandbox_name}},
    }


def _sandbox(
    ready=True,
    operating_mode='Running',
    reason=None,
    message=None,
    service_fqdn='sb-1.agents.svc.cluster.local',
):
    return {
        'spec': {'operatingMode': operating_mode},
        'status': {
            'serviceFQDN': service_fqdn,
            'conditions': [
                {
                    'type': 'Ready',
                    'status': 'True' if ready else 'False',
                    'reason': reason,
                    'message': message,
                }
            ],
        },
    }


async def test_start_sandbox_creates_claim(service, custom_objects):
    info = await service.start_sandbox()

    custom_objects.create_namespaced_custom_object.assert_called_once()
    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']

    assert body['kind'] == 'SandboxClaim'
    assert body['metadata']['namespace'] == 'agents'
    assert body['metadata']['labels'][MANAGED_BY_LABEL] == MANAGED_BY_VALUE
    assert body['metadata']['labels'][SANDBOX_ID_LABEL] == info.id
    # The warm pool falls back to the spec id when not pinned in config.
    assert body['spec']['warmPoolRef'] == {'name': 'openhands-pool'}
    assert body['spec'] == {'warmPoolRef': {'name': 'openhands-pool'}}
    annotations = body['metadata']['annotations']
    assert annotations[TEMPLATE_NAME_ANNOTATION] == 'openhands-agent-server'
    assert annotations[CREDENTIALS_SECRET_ANNOTATION] == body['metadata']['name']
    assert 'env' not in body['spec']
    assert 'volumeClaimTemplates' not in body['spec']

    secret = service._core_v1.create_namespaced_secret.call_args.kwargs['body']
    assert secret['metadata']['name'] == body['metadata']['name']
    assert secret['stringData'][SESSION_API_KEY_SECRET_KEY]
    assert secret['stringData'][SANDBOX_SECRET_KEY]
    assert (
        secret['stringData'][SESSION_API_KEY_SECRET_KEY]
        != secret['stringData'][SANDBOX_SECRET_KEY]
    )

    # A brand new sandbox is not reachable yet.
    assert info.status == SandboxStatus.STARTING
    assert info.session_api_key is None
    assert info.exposed_urls == []


async def test_start_sandbox_uses_configured_bootstrap_key_only_for_init_auth(
    service, custom_objects
):
    service.agent_server_secret_key = 'configured-shared-key'

    info = await service.start_sandbox()

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    assert 'env' not in body['spec']
    secret = service._core_v1.create_namespaced_secret.call_args.kwargs['body']
    assert secret['stringData'][SANDBOX_SECRET_KEY] != 'configured-shared-key'
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [_claim(sandbox_id=info.id)]
    }
    assert await service.get_sandbox_init_api_key(info.id) == 'configured-shared-key'


async def test_start_sandbox_requires_bootstrap_key_for_deferred_profile(
    service, custom_objects
):
    service.agent_server_secret_key = None
    service.sandbox_spec_service.get_default_sandbox_spec.return_value.initial_env = {
        'OH_DEFERRED_INIT': 'true'
    }

    with pytest.raises(SandboxError, match='shared bootstrap key'):
        await service.start_sandbox()

    custom_objects.create_namespaced_custom_object.assert_not_called()


async def test_windows_claim_is_pool_safe(
    service, mock_sandbox_spec_service, custom_objects
):
    windows = {spec.id: spec for spec in get_default_sandbox_specs()}[
        WINDOWS_STANDARD_POOL
    ]
    mock_sandbox_spec_service.get_sandbox_spec.return_value = windows

    await service.start_sandbox(
        sandbox_spec_id=WINDOWS_STANDARD_POOL, sandbox_id='windows-1'
    )

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    assert body['spec']['warmPoolRef'] == {'name': WINDOWS_STANDARD_POOL}
    assert 'volumeClaimTemplates' not in body['spec']
    assert 'env' not in body['spec']


async def test_start_sandbox_can_skip_session_key_injection(
    mock_sandbox_spec_service, custom_objects
):
    service = KubernetesSandboxService(
        sandbox_spec_service=mock_sandbox_spec_service,
        namespace='agents',
        warm_pool='pinned-pool',
        claim_name_prefix='oh-',
        sandbox_url_pattern='https://{sandbox_name}.example.com:{port}',
        webhook_base_url='http://app-server:3000',
        exposed_ports=[],
        max_num_sandboxes=5,
        inject_session_key=False,
        agent_server_secret_key='bootstrap-key',
        validate_profile_infrastructure=False,
        _core_v1=MagicMock(),
        _custom_objects=custom_objects,
    )

    await service.start_sandbox()

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    assert 'env' not in body['spec']
    secret = service._core_v1.create_namespaced_secret.call_args.kwargs['body']
    assert SESSION_API_KEY_SECRET_KEY not in secret['stringData']
    # An explicitly configured pool wins over the spec id.
    assert body['spec']['warmPoolRef'] == {'name': 'pinned-pool'}


async def test_claim_name_is_dns_safe(service, custom_objects):
    await service.start_sandbox(sandbox_id='Mixed_Case.Id')

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    name = body['metadata']['name']
    assert name.startswith('oh-agent-server-mixed-case-id-')
    assert re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', name), name
    # The label keeps the id verbatim so lookups stay exact.
    assert body['metadata']['labels'][SANDBOX_ID_LABEL] == 'Mixed_Case.Id'


async def test_sanitised_ids_do_not_collide(service, custom_objects):
    """Ids that sanitise to the same string must still get distinct claims."""
    await service.start_sandbox(sandbox_id='abc_def')
    first = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    await service.start_sandbox(sandbox_id='abc.def')
    second = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']

    assert first['metadata']['name'] != second['metadata']['name']


async def test_duplicate_id_is_reported_as_such(service, custom_objects):
    custom_objects.create_namespaced_custom_object.side_effect = ApiException(
        status=409, reason='Conflict'
    )

    with pytest.raises(SandboxError, match='already exists'):
        await service.start_sandbox(sandbox_id='dupe')


async def test_search_fans_out_claim_conversions(service, custom_objects):
    """Every claim is converted; the Sandbox reads happen concurrently."""
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [
            _claim(sandbox_id='sb1', sandbox_name='sb-1'),
            _claim(sandbox_id='sb2', sandbox_name='sb-2'),
        ]
    }
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    page = await service.search_sandboxes()

    assert sorted(item.id for item in page.items) == ['sb1', 'sb2']


async def test_search_batches_owner_lookup(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [
            _claim(sandbox_id='sb1', sandbox_name='sb-1'),
            _claim(sandbox_id='sb2', sandbox_name='sb-2'),
            {'metadata': {'labels': {}}},
        ]
    }
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()
    access_service = AsyncMock()
    access_service.owner_user_ids.return_value = {
        'sb1': 'entra:tenant:owner-1',
    }
    access_service.owner_user_id.side_effect = AssertionError(
        'search must not query owners per claim'
    )
    service.access_service = access_service

    page = await service.search_sandboxes()

    access_service.owner_user_ids.assert_awaited_once_with(['sb1', 'sb2'])
    access_service.owner_user_id.assert_not_awaited()
    assert {item.id: item.created_by_user_id for item in page.items} == {
        'sb1': 'entra:tenant:owner-1',
        'sb2': None,
    }


async def test_session_key_lookup_filters_server_side(service, custom_objects):
    """The session key is looked up through a label selector, not a full scan."""
    key = 'secret-key'
    claim = _claim(env=[{'name': SESSION_API_KEY_VARIABLE, 'value': key}])
    custom_objects.list_namespaced_custom_object.return_value = {'items': [claim]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    info = await service.get_sandbox_by_session_api_key(key)

    assert info is not None and info.id == 'sb1'
    selector = custom_objects.list_namespaced_custom_object.call_args.kwargs[
        'label_selector'
    ]
    assert SESSION_KEY_HASH_LABEL in selector
    assert key not in selector, 'the raw key must not be sent as a label value'


async def test_session_key_hash_collision_does_not_authenticate(
    service, custom_objects
):
    """A claim returned by the label filter still has to match the real key."""
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [_claim(env=[{'name': SESSION_API_KEY_VARIABLE, 'value': 'other'}])]
    }

    assert await service.get_sandbox_by_session_api_key('secret-key') is None


async def test_start_sandbox_labels_the_session_key_hash(service, custom_objects):
    await service.start_sandbox()

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    secret = service._core_v1.create_namespaced_secret.call_args.kwargs['body']
    session_key = secret['stringData'][SESSION_API_KEY_SECRET_KEY]
    labels = body['metadata']['labels']
    assert labels[SESSION_KEY_HASH_LABEL] == KubernetesSandboxService._session_key_hash(
        session_key
    )
    # The label is an index, never the secret itself.
    assert labels[SESSION_KEY_HASH_LABEL] != session_key


async def test_get_sandbox_running_exposes_urls_and_key(service, custom_objects):
    service.exposed_ports = [
        type('P', (), {'name': AGENT_SERVER, 'container_port': 8000})()
    ]
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [
            _claim(env=[{'name': SESSION_API_KEY_VARIABLE, 'value': 'secret-key'}])
        ]
    }
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.status == SandboxStatus.RUNNING
    assert info.session_api_key == 'secret-key'
    # DNS resolves the Sandbox resource name, not the app-level id.
    assert info.exposed_urls[0].url == 'https://sb-1.example.com:8000'


async def test_get_sandbox_init_api_key_returns_shared_bootstrap_secret(
    service, custom_objects
):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}

    assert await service.get_sandbox_init_api_key('sb1') == 'bootstrap-key'


async def test_get_sandbox_secret_key_reads_claim_owned_secret(
    service, custom_objects, core_v1
):
    claim = _claim()
    claim['metadata']['annotations'][CREDENTIALS_SECRET_ANNOTATION] = 'credentials'
    custom_objects.list_namespaced_custom_object.return_value = {'items': [claim]}
    core_v1.read_namespaced_secret.return_value = {
        'data': {
            SANDBOX_SECRET_KEY: base64.b64encode(b'sandbox-secret').decode(),
        }
    }

    assert await service.get_sandbox_secret_key('sb1') == 'sandbox-secret'


async def test_get_sandbox_internal_url_uses_controller_service_fqdn(
    service, custom_objects
):
    service.exposed_ports = [
        type('P', (), {'name': AGENT_SERVER, 'container_port': 8000})()
    ]
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    url = await service.get_sandbox_internal_url('sb1', AGENT_SERVER)

    assert url == 'http://sb-1.agents.svc.cluster.local:8000'


async def test_get_sandbox_internal_url_requires_service_fqdn(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox(
        service_fqdn=None
    )

    with pytest.raises(SandboxError, match='no internal service address'):
        await service.get_sandbox_internal_url('sb1', AGENT_SERVER)


async def test_get_legacy_sandbox_adopts_warm_pool_profile(service, custom_objects):
    claim = _claim()
    claim['metadata']['annotations'] = {}
    claim['spec']['warmPoolRef'] = {'name': 'openhands-pool'}
    custom_objects.list_namespaced_custom_object.return_value = {'items': [claim]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.sandbox_spec_id == 'openhands-pool'
    service.sandbox_spec_service.get_sandbox_spec.assert_awaited_once_with(
        'openhands-pool', include_unavailable=True
    )


async def test_get_sandbox_missing_returns_none(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': []}
    assert await service.get_sandbox('nope') is None


async def test_suspended_sandbox_is_paused(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox(
        ready=False, operating_mode='Suspended'
    )

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.status == SandboxStatus.PAUSED
    # A paused sandbox must not advertise a key or URLs.
    assert info.session_api_key is None
    assert info.exposed_urls == []


async def test_failed_sandbox_reports_error_with_detail(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox(
        ready=False, reason='PodFailed', message='ImagePullBackOff'
    )

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.status == SandboxStatus.ERROR
    assert info.status_detail == 'ImagePullBackOff'


@pytest.mark.parametrize(
    ('sandbox', 'pods', 'pvcs', 'events', 'phase', 'reason'),
    [
        (
            None,
            [],
            [],
            [{'reason': 'TriggeredScaleUp', 'message': 'scale-up started'}],
            SandboxStartupPhase.PROVISIONING_NODE,
            'AUTOSCALER_TRIGGERED',
        ),
        (
            None,
            [],
            [],
            [
                {
                    'reason': 'FailedScheduling',
                    'message': '0/1 nodes available: 1 Insufficient cpu.',
                }
            ],
            SandboxStartupPhase.WAITING_FOR_CAPACITY,
            'CAPACITY_UNAVAILABLE',
        ),
        (
            None,
            [],
            [
                {
                    'metadata': {'name': 'workspace-pvc'},
                    'status': {'phase': 'Pending'},
                }
            ],
            [],
            SandboxStartupPhase.PROVISIONING_STORAGE,
            'WORKSPACE_STORAGE_PENDING',
        ),
        (
            None,
            [
                {
                    'metadata': {'name': 'pod-1'},
                    'spec': {'nodeName': 'node-1'},
                    'status': {'phase': 'Pending'},
                }
            ],
            [],
            [{'reason': 'Pulling', 'message': 'Pulling image'}],
            SandboxStartupPhase.PULLING_IMAGE,
            'IMAGE_PULLING',
        ),
        (
            None,
            [
                {
                    'metadata': {'name': 'pod-1'},
                    'spec': {'nodeName': 'node-1'},
                    'status': {
                        'phase': 'Pending',
                        'containerStatuses': [
                            {
                                'state': {'waiting': {'reason': 'ContainerCreating'}},
                                'restartCount': 0,
                            }
                        ],
                    },
                }
            ],
            [],
            [],
            SandboxStartupPhase.STARTING_CONTAINER,
            'CONTAINER_STARTING',
        ),
        (
            None,
            [
                {
                    'metadata': {'name': 'pod-1'},
                    'spec': {'nodeName': 'node-1'},
                    'status': {
                        'phase': 'Running',
                        'containerStatuses': [
                            {
                                'state': {
                                    'running': {'startedAt': '2026-11-16T21:00:00Z'}
                                },
                                'restartCount': 0,
                            }
                        ],
                    },
                }
            ],
            [],
            [],
            SandboxStartupPhase.WAITING_FOR_AGENT_SERVER,
            'AGENT_SERVER_NOT_READY',
        ),
        (
            _sandbox(ready=True),
            [],
            [],
            [],
            SandboxStartupPhase.READY,
            'SANDBOX_READY',
        ),
    ],
)
def test_kubernetes_startup_progress_phases(
    service,
    sandbox,
    pods,
    pvcs,
    events,
    phase,
    reason,
):
    progress = service._startup_progress_from(
        _claim(), sandbox, pods, pvcs, events
    ).progress

    assert progress.phase == phase
    assert progress.reason == reason
    assert progress.status == (
        SandboxStartupProgressStatus.READY
        if phase == SandboxStartupPhase.READY
        else SandboxStartupProgressStatus.PROGRESSING
    )


def test_kubernetes_startup_progress_distinguishes_durable_storage(service):
    claim = _claim()
    claim['metadata']['annotations'][DURABLE_PVC_ANNOTATION] = (
        'openhands-halo-conversations'
    )

    progress = service._startup_progress_from(
        claim,
        None,
        [],
        [
            {
                'metadata': {'name': 'openhands-halo-conversations'},
                'status': {'phase': 'Pending'},
            }
        ],
        [],
    ).progress

    assert progress.phase == SandboxStartupPhase.PROVISIONING_STORAGE
    assert progress.reason == 'DURABLE_STORAGE_PENDING'


def test_kubernetes_startup_progress_fails_terminal_image_error(service):
    observation = service._startup_progress_from(
        _claim(),
        None,
        [
            {
                'metadata': {'name': 'pod-1'},
                'spec': {'nodeName': 'node-1'},
                'status': {
                    'phase': 'Pending',
                    'containerStatuses': [
                        {
                            'state': {'waiting': {'reason': 'ImagePullBackOff'}},
                            'restartCount': 0,
                        }
                    ],
                },
            }
        ],
        [],
        [],
    )
    progress = observation.progress

    assert progress.phase == SandboxStartupPhase.FAILED
    assert progress.status == SandboxStartupProgressStatus.FAILED
    assert progress.reason == 'IMAGE_PULL_BACKOFF'
    assert observation.failure_detail == 'ImagePullBackOff'
    assert 'registry' not in progress.message.lower()


def test_repeated_scheduler_event_is_not_forward_progress(service):
    first = service._startup_progress_from(
        _claim(),
        None,
        [],
        [],
        [
            {
                'reason': 'FailedScheduling',
                'message': '0/1 nodes available: 1 Insufficient cpu.',
                'count': 1,
                'lastTimestamp': '2026-11-16T21:00:00Z',
            }
        ],
    )
    repeated = service._startup_progress_from(
        _claim(),
        None,
        [],
        [],
        [
            {
                'reason': 'FailedScheduling',
                'message': '0/1 nodes available: 1 Insufficient cpu.',
                'count': 20,
                'lastTimestamp': '2026-11-16T21:05:00Z',
            }
        ],
    )

    assert first.progress_key == repeated.progress_key


def test_recovered_storage_event_is_not_terminal(service):
    progress = service._startup_progress_from(
        _claim(),
        None,
        [],
        [
            {
                'metadata': {'name': 'workspace-pvc'},
                'status': {'phase': 'Bound'},
            }
        ],
        [
            {'reason': 'ProvisioningFailed', 'message': 'temporary failure'},
            {'reason': 'ProvisioningSucceeded', 'message': 'provisioned'},
        ],
    ).progress

    assert progress.status == SandboxStartupProgressStatus.PROGRESSING
    assert progress.phase != SandboxStartupPhase.FAILED


@pytest.mark.asyncio
async def test_transient_topology_provisioning_failure_continues_until_ready(service):
    transient = service._startup_progress_from(
        _claim(),
        None,
        [],
        [
            {
                'metadata': {'name': 'workspace-pvc'},
                'status': {'phase': 'Pending'},
            }
        ],
        [
            {
                'reason': 'ProvisioningFailed',
                'message': (
                    'error generating accessibility requirements: '
                    'no topology key found for node akshalo2500000e'
                ),
            }
        ],
    )
    ready = service._startup_progress_from(
        _claim(),
        _sandbox(ready=True),
        [],
        [
            {
                'metadata': {'name': 'workspace-pvc'},
                'status': {'phase': 'Bound'},
            }
        ],
        [
            {
                'reason': 'ProvisioningFailed',
                'message': (
                    'error generating accessibility requirements: '
                    'no topology key found for node akshalo2500000e'
                ),
            },
            {'reason': 'ProvisioningSucceeded', 'message': 'provisioned'},
        ],
    )
    service.get_sandbox_startup_observation = AsyncMock(side_effect=[transient, ready])
    service.get_sandbox = AsyncMock(return_value=MagicMock(exposed_urls=[]))

    progress = [
        item
        async for item in service.watch_sandbox_startup(
            '6NM1Of5uSwiXtRgUXT4n84',
            timeout=10,
            inactivity_timeout=10,
            poll_interval=0,
        )
    ]

    assert [item.status for item in progress] == [
        SandboxStartupProgressStatus.PROGRESSING,
        SandboxStartupProgressStatus.READY,
    ]
    assert progress[0].phase == SandboxStartupPhase.PROVISIONING_STORAGE
    assert progress[0].reason == 'WORKSPACE_STORAGE_PENDING'


def test_terminal_storage_provisioning_failure_remains_terminal(service):
    observation = service._startup_progress_from(
        _claim(),
        None,
        [],
        [],
        [
            {
                'reason': 'ProvisioningFailed',
                'message': (
                    'failed to provision volume: rpc error: code = InvalidArgument '
                    'desc = invalid storage account configuration'
                ),
            }
        ],
    )

    assert observation.progress.status == SandboxStartupProgressStatus.FAILED
    assert observation.progress.reason == 'STORAGE_PROVISIONING_FAILED'
    assert 'invalid storage account configuration' in (observation.failure_detail or '')


def test_terminal_storage_event_has_safe_structured_reason(service):
    observation = service._startup_progress_from(
        _claim(),
        None,
        [],
        [],
        [
            {
                'reason': 'FailedMount',
                'message': 'failed to mount secret internal-share-name',
            }
        ],
    )
    progress = observation.progress

    assert progress.status == SandboxStartupProgressStatus.FAILED
    assert progress.reason == 'STORAGE_MOUNT_FAILED'
    assert 'internal-share-name' not in progress.message
    assert 'internal-share-name' in (observation.failure_detail or '')


def test_autoscaler_waits_for_matching_node_group(service):
    observation = service._startup_progress_from(
        _claim(),
        None,
        [],
        [],
        [
            {
                'reason': 'FailedScheduling',
                'message': 'node(s) did not match Pod node affinity',
            },
            {
                'reason': 'NotTriggerScaleUp',
                'message': 'node(s) did not match Pod node affinity',
            },
        ],
    )

    assert observation.progress.status == SandboxStartupProgressStatus.PROGRESSING
    assert observation.progress.reason == 'POD_UNSCHEDULED'
    assert observation.failure_detail is None


def test_kubernetes_startup_resources_follow_owners_and_events(service, core_v1):
    claim = _claim()
    claim['metadata']['name'] = 'claim-1'
    sandbox = {
        'metadata': {'name': 'sandbox-1'},
        'status': {'conditions': []},
    }
    pod = MagicMock()
    pod.to_dict.return_value = {
        'metadata': {
            'name': 'pod-1',
            'owner_references': [{'name': 'sandbox-1'}],
        },
        'status': {'phase': 'Pending'},
    }
    workspace_pvc = MagicMock()
    workspace_pvc.to_dict.return_value = {
        'metadata': {
            'name': 'workspace-pvc',
            'owner_references': [{'name': 'sandbox-1'}],
        },
        'status': {'phase': 'Pending'},
    }
    durable_pvc = MagicMock()
    durable_pvc.to_dict.return_value = {
        'metadata': {'name': 'durable-pvc'},
        'status': {'phase': 'Bound'},
    }
    event = MagicMock()
    event.to_dict.return_value = {
        'metadata': {'uid': 'event-1', 'name': 'event-1'},
        'reason': 'TriggeredScaleUp',
        'message': 'scale-up started',
    }
    core_v1.list_namespaced_pod.return_value.items = [pod]
    core_v1.list_namespaced_persistent_volume_claim.return_value.items = [
        workspace_pvc,
        durable_pvc,
    ]
    core_v1.list_namespaced_event.return_value.items = [event]

    pods, pvcs, events = service._get_startup_resources_sync(claim, sandbox)

    assert [_resource['metadata']['name'] for _resource in pods] == ['pod-1']
    assert {_resource['metadata']['name'] for _resource in pvcs} == {'workspace-pvc'}
    assert events[0]['reason'] == 'TriggeredScaleUp'
    selectors = {
        call.kwargs['field_selector']
        for call in core_v1.list_namespaced_event.call_args_list
    }
    assert selectors == {
        'involvedObject.name=claim-1',
        'involvedObject.name=sandbox-1',
        'involvedObject.name=pod-1',
        'involvedObject.name=workspace-pvc',
    }


async def test_pause_and_resume_patch_operating_mode(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}

    assert await service.pause_sandbox('sb1') is True
    body = custom_objects.patch_namespaced_custom_object.call_args.kwargs['body']
    assert body == {'spec': {'operatingMode': 'Suspended'}}

    assert await service.resume_sandbox('sb1') is True
    body = custom_objects.patch_namespaced_custom_object.call_args.kwargs['body']
    assert body == {'spec': {'operatingMode': 'Running'}}


async def test_pause_unknown_sandbox_returns_false(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': []}
    assert await service.pause_sandbox('nope') is False


async def test_get_sandbox_by_session_api_key(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [
            _claim(sandbox_id='other', env=[]),
            _claim(env=[{'name': SESSION_API_KEY_VARIABLE, 'value': 'k'}]),
        ]
    }
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    info = await service.get_sandbox_by_session_api_key('k')
    assert info is not None and info.id == 'sb1'

    record = await service.get_sandbox_record_by_session_api_key('k')
    assert record is not None and record.id == 'sb1'

    assert await service.get_sandbox_by_session_api_key('missing') is None


async def test_persisted_owner_authorizes_conversation_webhook(service, custom_objects):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(StoredSandboxAccess.__table__.create)
        await connection.run_sync(StoredConversationMetadata.__table__.create)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_maker() as db_session:
            owner_id = 'entra:tenant-id:object-id'
            service.access_service = SandboxAccessService(
                db_session=db_session,
                user_id=owner_id,
            )

            info = await service.start_sandbox(sandbox_id='owned-sandbox')
            await db_session.commit()
            body = custom_objects.create_namespaced_custom_object.call_args.kwargs[
                'body'
            ]
            custom_objects.list_namespaced_custom_object.return_value = {
                'items': [body]
            }

            env = {item['name']: item['value'] for item in body['spec']['env']}
            record = await service.get_sandbox_record_by_session_api_key(
                env[SESSION_API_KEY_VARIABLE]
            )

            assert info.created_by_user_id == owner_id
            assert record is not None
            assert record.created_by_user_id == owner_id

            from openhands.app_server.event_callback.webhook_router import (
                valid_conversation,
            )

            conversation_info = MagicMock(
                created_by_user_id=owner_id,
                sandbox_id='owned-sandbox',
            )
            conversation_service = AsyncMock()
            conversation_service.get_app_conversation_info.return_value = (
                conversation_info
            )

            @contextlib.asynccontextmanager
            async def get_conversation_service(state):
                yield conversation_service

            with patch(
                'openhands.app_server.event_callback.webhook_router.get_app_conversation_info_service',
                get_conversation_service,
            ):
                result = await valid_conversation(
                    conversation_id=uuid4(),
                    sandbox_record=record,
                )

            assert result is conversation_info
    finally:
        await engine.dispose()


async def test_delete_sandbox(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}

    assert await service.delete_sandbox('sb1') is True
    assert (
        custom_objects.delete_namespaced_custom_object.call_args.kwargs['name']
        == 'oh-agent-server-sb1'
    )

    custom_objects.list_namespaced_custom_object.return_value = {'items': []}
    assert await service.delete_sandbox('sb1') is False


async def test_reconcile_orphaned_sandboxes_preserves_active_claims(
    service, custom_objects
):
    active = _claim()
    orphan = _claim()
    orphan['metadata']['name'] = 'oh-agent-server-orphan'
    orphan['metadata']['labels'][SANDBOX_ID_LABEL] = 'orphan'
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [active, orphan]
    }

    deleted = await service.reconcile_orphaned_sandboxes({'sb1'})

    assert deleted == ['orphan']
    assert (
        custom_objects.delete_namespaced_custom_object.call_args.kwargs['name']
        == 'oh-agent-server-orphan'
    )


async def test_creation_reconciliation_removes_access_for_orphans(
    service, custom_objects
):
    orphan = _claim()
    custom_objects.list_namespaced_custom_object.return_value = {'items': [orphan]}
    access_service = AsyncMock()
    service.access_service = access_service

    with patch(
        'openhands.app_server.sandbox.sandbox_lifecycle.active_sandbox_ids',
        new=AsyncMock(return_value=set()),
    ):
        await service._reconcile_orphaned_capacity()

    access_service.delete_sandbox.assert_awaited_once_with('sb1')


async def test_creation_reconciliation_preserves_inflight_start_claim(
    service, custom_objects
):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(StoredAppConversationStartTask.__table__.create)
        await connection.run_sync(StoredConversationMetadata.__table__.create)
        await connection.run_sync(StoredSandboxAccess.__table__.create)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_maker() as db_session:
            start_task_service = SQLAppConversationStartTaskService(db_session)
            await start_task_service.save_app_conversation_start_task(
                AppConversationStartTask(
                    created_by_user_id='user',
                    status=AppConversationStartTaskStatus.WAITING_FOR_SANDBOX,
                    sandbox_id='first-start',
                    request=AppConversationStartRequest(),
                )
            )
            await start_task_service.save_app_conversation_start_task(
                AppConversationStartTask(
                    created_by_user_id='user',
                    status=AppConversationStartTaskStatus.ERROR,
                    sandbox_id='failed-start',
                    request=AppConversationStartRequest(),
                )
            )
            active = _claim(sandbox_id='first-start')
            failed = _claim(sandbox_id='failed-start')
            orphan = _claim(sandbox_id='orphan')
            custom_objects.list_namespaced_custom_object.return_value = {
                'items': [active, failed, orphan]
            }
            service.access_service = SandboxAccessService(
                db_session=db_session,
                user_id='user',
            )

            await service._reconcile_orphaned_capacity()

            deleted_claims = {
                call.kwargs['name']
                for call in custom_objects.delete_namespaced_custom_object.call_args_list
            }
            assert deleted_claims == {
                'oh-agent-server-failed-start',
                'oh-agent-server-orphan',
            }
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    'reason,expected',
    [
        ('PodFailed', SandboxStatus.ERROR),
        ('PodSucceeded', SandboxStatus.ERROR),
        ('SandboxExpired', SandboxStatus.ERROR),
        ('DependenciesNotReady', SandboxStatus.STARTING),
    ],
)
async def test_terminal_ready_reasons(service, custom_objects, reason, expected):
    """The reasons the controller actually emits must not read as STARTING."""
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox(
        ready=False, reason=reason, message='detail'
    )

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.status == expected


@pytest.mark.parametrize(
    'bad_id',
    [
        'has,comma',
        'has=equals',
        'a' * 64,
        '',
        '-leading-dash',
    ],
)
async def test_invalid_sandbox_ids_are_rejected(service, custom_objects, bad_id):
    """Ids travel as label values, so a selector-breaking id must not be accepted."""
    with pytest.raises(SandboxError, match='Invalid sandbox id'):
        await service.start_sandbox(sandbox_id=bad_id)

    custom_objects.create_namespaced_custom_object.assert_not_called()


async def test_claim_name_fits_dns_label_limit(service, custom_objects):
    await service.start_sandbox(sandbox_id='s' * 63)

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    name = body['metadata']['name']
    # On a cold start this is also the Sandbox and Service name.
    assert len(name) <= 63, name
    assert re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', name), name


async def test_malformed_page_id_starts_from_the_beginning(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    page = await service.search_sandboxes(page_id='not-a-number')

    assert [item.id for item in page.items] == ['sb1']


@pytest.mark.parametrize('bad_id', ['has,comma', 'has=equals', 'a' * 64])
async def test_lookups_treat_unusable_ids_as_missing(service, custom_objects, bad_id):
    """An id that would corrupt the selector must 404, not reach the API server."""
    assert await service.get_sandbox(bad_id) is None
    assert await service.pause_sandbox(bad_id) is False
    assert await service.resume_sandbox(bad_id) is False
    assert await service.delete_sandbox(bad_id) is False

    custom_objects.list_namespaced_custom_object.assert_not_called()


async def test_invalid_id_does_not_pause_existing_sandboxes(service, custom_objects):
    """Validation runs before eviction, so a bad request has no side effects."""
    with pytest.raises(SandboxError, match='Invalid sandbox id'):
        await service.start_sandbox(sandbox_id='has,comma')

    custom_objects.patch_namespaced_custom_object.assert_not_called()


async def test_claim_name_fits_limit_with_a_long_prefix(
    mock_sandbox_spec_service, custom_objects, core_v1
):
    service = KubernetesSandboxService(
        sandbox_spec_service=mock_sandbox_spec_service,
        namespace='agents',
        warm_pool=None,
        claim_name_prefix='p' * 57,
        sandbox_url_pattern='https://{sandbox_name}.example.com:{port}',
        webhook_base_url='http://app-server:3000',
        exposed_ports=[],
        max_num_sandboxes=5,
        validate_profile_infrastructure=False,
        _custom_objects=custom_objects,
        _core_v1=core_v1,
    )

    await service.start_sandbox(sandbox_id='s' * 63)

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    assert len(body['metadata']['name']) <= 63


def test_injector_rejects_a_prefix_that_cannot_fit_the_digest():
    with pytest.raises(ValidationError):
        KubernetesSandboxServiceInjector(claim_name_prefix='p' * 58)


async def test_unknown_spec_id_does_not_pause_existing_sandboxes(
    service, mock_sandbox_spec_service, custom_objects
):
    """An unknown spec id is a hard error, so it must not evict anything first."""
    mock_sandbox_spec_service.get_sandbox_spec.return_value = None

    with pytest.raises(ValueError, match='not found'):
        await service.start_sandbox(sandbox_spec_id='no-such-spec')

    custom_objects.patch_namespaced_custom_object.assert_not_called()
    custom_objects.create_namespaced_custom_object.assert_not_called()


def test_default_kubernetes_specs_include_curated_linux_and_windows_profiles():
    specs = {spec.id: spec for spec in get_default_sandbox_specs()}

    linux = specs[DEFAULT_WARM_POOL]
    assert linux.operating_system == SandboxOperatingSystem.LINUX
    assert linux.resource_class == SandboxResourceClass.STANDARD
    assert linux.working_dir == '/workspace/project'
    assert linux.workspace_mount_path == '/workspace'
    assert linux.durable_state_dir == '/workspace/.openhands'
    assert linux.durable_mount_path == '/workspace'
    assert linux.startup_timeout_seconds == 300
    assert linux.initial_env['OH_WORKSPACE_PATH'] == '/workspace/project'
    assert linux.initial_env['OH_CONVERSATIONS_PATH'] == (
        '/workspace/.openhands/conversations'
    )
    assert linux.initial_env['OH_CONVERSATION_WORKTREE_ROOT'] == (
        '/workspace/worktrees'
    )

    windows = specs[WINDOWS_STANDARD_POOL]
    assert windows.operating_system == SandboxOperatingSystem.WINDOWS
    assert windows.resource_class == SandboxResourceClass.STANDARD
    assert windows.working_dir == r'C:\workspace\project'
    assert windows.workspace_mount_path == r'C:\workspace'
    assert windows.durable_state_dir == r'C:\workspace\.openhands'
    assert windows.durable_mount_path == r'C:\workspace'
    assert windows.initial_env['OH_CONVERSATIONS_PATH'] == (
        r'C:\workspace\.openhands\conversations'
    )
    assert windows.initial_env['OH_PERSISTENCE_DIR'] == (
        r'C:\workspace\.openhands\persistence'
    )
    assert windows.initial_env['OH_CONVERSATION_WORKTREE_ROOT'] == (
        r'C:\workspace\worktrees'
    )
    assert windows.workspace_storage_class == 'managed-csi'
    assert windows.durable_storage_class is None
    assert windows.durable_volume_claim_name is None
    assert windows.cpu_cores == 4
    assert windows.memory_gib == 8
    assert windows.startup_timeout_seconds == 600


def test_kubernetes_manifests_materialize_profile_storage_and_placement():
    repo_root = Path(__file__).parents[3]
    templates = {
        document['metadata']['name']: document
        for document in yaml.safe_load_all(
            (repo_root / 'deploy/kubernetes/sandboxtemplate.yaml').read_text()
        )
    }
    pools = {
        document['metadata']['name']: document
        for document in yaml.safe_load_all(
            (repo_root / 'deploy/kubernetes/sandboxwarmpool.yaml').read_text()
        )
    }

    linux = templates['openhands-agent-server']
    assert 'volumeClaimTemplatesPolicy' not in linux['spec']
    assert 'envVarsInjectionPolicy' not in linux['spec']
    assert (
        linux['spec']['volumeClaimTemplates'][0]['spec']['storageClassName']
        == 'managed-csi'
    )
    linux_pod = linux['spec']['podTemplate']['spec']
    assert linux_pod['containers'][0]['workingDir'] == '/workspace/project'
    assert 'volumes' not in linux_pod
    linux_env = {item['name']: item for item in linux_pod['containers'][0]['env']}
    assert linux_env['OH_DEFERRED_INIT']['value'] == 'true'
    assert linux_env['OH_SECRET_KEY']['valueFrom']['secretKeyRef'] == {
        'name': 'openhands-agent-server-bootstrap',
        'key': 'secret-key',
    }
    linux_mounts = linux_pod['containers'][0]['volumeMounts']
    assert {
        'name': 'workspace',
        'mountPath': '/workspace',
    } in linux_mounts
    assert len(linux_mounts) == 1

    windows = templates['openhands-agent-server-windows-2025']
    assert 'volumeClaimTemplatesPolicy' not in windows['spec']
    assert 'envVarsInjectionPolicy' not in windows['spec']
    windows_pod = windows['spec']['podTemplate']['spec']
    assert windows_pod['containers'][0]['workingDir'] == r'C:\workspace\project'
    assert windows_pod['nodeSelector'] == {
        'kubernetes.io/os': 'windows',
        'node.kubernetes.io/windows-build': '10.0.26100',
    }
    assert windows_pod['tolerations'] == [
        {
            'key': 'os',
            'operator': 'Equal',
            'value': 'windows',
            'effect': 'NoSchedule',
        }
    ]
    assert 'volumes' not in windows_pod
    windows_mounts = windows_pod['containers'][0]['volumeMounts']
    assert {
        'name': 'workspace',
        'mountPath': r'C:\workspace',
    } in windows_mounts
    assert len(windows_mounts) == 1
    assert all(
        'subPath' not in mount and 'subPathExpr' not in mount
        for mount in windows_mounts
    )

    assert pools['openhands-pool']['spec']['sandboxTemplateRef']['name'] == (
        'openhands-agent-server'
    )
    assert pools['openhands-pool']['spec']['replicas'] == 1
    assert pools[WINDOWS_STANDARD_POOL]['spec']['sandboxTemplateRef']['name'] == (
        'openhands-agent-server-windows-2025'
    )


def test_profile_infrastructure_validation_reads_selected_pool_and_template(
    service, custom_objects, core_v1
):
    repo_root = Path(__file__).parents[3]
    templates = {
        document['metadata']['name']: document
        for document in yaml.safe_load_all(
            (repo_root / 'deploy/kubernetes/sandboxtemplate.yaml').read_text()
        )
    }
    pools = {
        document['metadata']['name']: document
        for document in yaml.safe_load_all(
            (repo_root / 'deploy/kubernetes/sandboxwarmpool.yaml').read_text()
        )
    }
    windows = {spec.id: spec for spec in get_default_sandbox_specs()}[
        WINDOWS_STANDARD_POOL
    ].model_copy(
        update={
            'image': templates['openhands-agent-server-windows-2025']['spec'][
                'podTemplate'
            ]['spec']['containers'][0]['image']
        }
    )

    def get_object(*, plural, name, **_):
        if plural == 'sandboxwarmpools':
            return pools[name]
        if plural == 'sandboxtemplates':
            return templates[name]
        raise AssertionError(plural)

    custom_objects.get_namespaced_custom_object.side_effect = get_object

    service._validate_profile_infrastructure_sync(windows, WINDOWS_STANDARD_POOL)
    core_v1.read_namespaced_persistent_volume_claim.assert_not_called()


def test_profile_infrastructure_validation_rejects_workspace_storage_drift(
    service, custom_objects
):
    repo_root = Path(__file__).parents[3]
    template = next(
        document
        for document in yaml.safe_load_all(
            (repo_root / 'deploy/kubernetes/sandboxtemplate.yaml').read_text()
        )
        if document['metadata']['name'] == 'openhands-agent-server'
    )
    linux = {spec.id: spec for spec in get_default_sandbox_specs()}[
        DEFAULT_WARM_POOL
    ].model_copy(update={'workspace_storage_class': 'wrong-storage-class'})
    custom_objects.get_namespaced_custom_object.side_effect = [
        {'spec': {'sandboxTemplateRef': {'name': 'openhands-agent-server'}}},
        template,
    ]

    with pytest.raises(SandboxError, match='workspace storage class'):
        service._validate_profile_infrastructure_sync(linux, DEFAULT_WARM_POOL)


def test_profile_infrastructure_validation_rejects_working_directory_drift(
    service, custom_objects
):
    repo_root = Path(__file__).parents[3]
    template = next(
        document
        for document in yaml.safe_load_all(
            (repo_root / 'deploy/kubernetes/sandboxtemplate.yaml').read_text()
        )
        if document['metadata']['name'] == 'openhands-agent-server'
    )
    linux = {spec.id: spec for spec in get_default_sandbox_specs()}[
        DEFAULT_WARM_POOL
    ].model_copy(update={'working_dir': '/wrong'})
    custom_objects.get_namespaced_custom_object.side_effect = [
        {'spec': {'sandboxTemplateRef': {'name': 'openhands-agent-server'}}},
        template,
    ]

    with pytest.raises(SandboxError, match='working directory'):
        service._validate_profile_infrastructure_sync(linux, DEFAULT_WARM_POOL)


def test_profile_infrastructure_validation_rejects_separate_durable_mount(
    service, custom_objects
):
    repo_root = Path(__file__).parents[3]
    template = next(
        document
        for document in yaml.safe_load_all(
            (repo_root / 'deploy/kubernetes/sandboxtemplate.yaml').read_text()
        )
        if document['metadata']['name'] == 'openhands-agent-server'
    )
    linux = {spec.id: spec for spec in get_default_sandbox_specs()}[
        DEFAULT_WARM_POOL
    ].model_copy(update={'durable_mount_path': '/separate'})
    custom_objects.get_namespaced_custom_object.side_effect = [
        {'spec': {'sandboxTemplateRef': {'name': 'openhands-agent-server'}}},
        template,
    ]
    with pytest.raises(SandboxError, match='adopted workspace volume'):
        service._validate_profile_infrastructure_sync(linux, DEFAULT_WARM_POOL)


@pytest.mark.parametrize('startup_timeout_seconds', [0, 3601])
def test_execution_profile_rejects_out_of_range_startup_timeout(
    startup_timeout_seconds,
):
    with pytest.raises(ValidationError):
        SandboxSpecInfo(
            id='invalid-timeout',
            command=None,
            startup_timeout_seconds=startup_timeout_seconds,
        )


def test_windows_profile_accepts_digest_pinned_halo_registry_image():
    image = (
        'ghcr.io/ghermann-msft/agent-server:abc123-windows-'
        'ltsc2025-amd64@sha256:' + '1' * 64
    )

    profile = SandboxSpecInfo(
        id=WINDOWS_STANDARD_POOL,
        command=None,
        operating_system=SandboxOperatingSystem.WINDOWS,
        platform='windows/amd64',
        image=image,
    )

    assert profile.image == image


def test_bootstrap_file_overrides_builtin_profile(tmp_path):
    bootstrap = tmp_path / 'profiles.json'
    bootstrap.write_text(
        '[{"id":"openhands-pool","command":null,'
        '"bootstrap_revision":"ppe-v2","working_dir":"/fast/project",'
        '"durable_state_dir":"/fast/.openhands",'
        '"durable_mount_path":"/fast"}]',
        encoding='utf-8',
    )
    with patch.dict('os.environ', {'OH_SANDBOX_SPEC_BOOTSTRAP_FILE': str(bootstrap)}):
        profiles = {profile.id: profile for profile in get_bootstrap_sandbox_specs()}

    overridden = profiles[DEFAULT_WARM_POOL]
    assert overridden.working_dir == '/fast/project'
    assert overridden.bootstrap_revision == 'ppe-v2'
    assert overridden.source == 'bootstrap'
    assert overridden.immutable is True


async def test_profile_global_quota_fails_before_claim_creation(
    service, mock_sandbox_spec_service, custom_objects
):
    spec = get_default_sandbox_specs()[0].model_copy(
        update={'max_concurrent_sandboxes': 1}
    )
    mock_sandbox_spec_service.get_default_sandbox_spec.return_value = spec
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [_claim(sandbox_id='existing')]
    }

    with pytest.raises(SandboxError, match='global concurrency limit'):
        await service.start_sandbox()

    custom_objects.create_namespaced_custom_object.assert_not_called()
