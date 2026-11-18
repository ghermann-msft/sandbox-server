import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxError, SandboxStartupTimeoutError
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    SandboxInfo,
    SandboxPage,
    SandboxRecord,
    SandboxStartupPhase,
    SandboxStartupProgress,
    SandboxStartupProgressStatus,
    SandboxStatus,
)
from openhands.app_server.services.injector import Injector
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)
from openhands.sdk.utils.models import DiscriminatedUnionMixin
from openhands.sdk.utils.paging import page_iterator

_logger = logging.getLogger(__name__)

SESSION_API_KEY_VARIABLE = 'OH_SESSION_API_KEYS_0'
WEBHOOK_CALLBACK_VARIABLE = 'OH_WEBHOOKS_0_BASE_URL'
ALLOW_CORS_ORIGINS_VARIABLE = 'OH_ALLOW_CORS_ORIGINS_0'

# Known start-failure classes we translate into short, user-safe messages. Raw
# runtime status_detail (k8s pod/scheduling text) can leak internal registry
# hosts, secret/configmap names, node taints/labels, cluster size, etc., so it
# is logged for debugging but never surfaced to end users. Only the standard
# resource names below are echoed back; extended/device resources are not (the
# name can reveal the device plugin in use).
_CAPACITY_MARKERS = (
    'Insufficient cpu',
    'Insufficient memory',
    'Insufficient ephemeral-storage',
    'Insufficient pods',
)
_IMAGE_PULL_MARKERS = ('ImagePullBackOff', 'ErrImagePull')
_STORAGE_MARKERS = ('FailedAttachVolume', 'FailedMount', 'ProvisioningFailed')
# Deterministic placement failures -- retrying won't help. Substrings match the
# scheduler's predicate messages without echoing specific taint/label values.
_SCHEDULING_MARKERS = (
    'untolerated taint',
    'node affinity',
    'node selector',
    'volume node affinity conflict',
    'topology spread',
    'unbound',  # e.g. "unbound immediate PersistentVolumeClaims"
)

_GENERIC_START_FAILURE = (
    'The sandbox failed to start for an unexpected reason. Please try again.'
)
_STARTUP_PHASE_MESSAGES = {
    SandboxStartupPhase.WAITING_FOR_CAPACITY: (
        'Waiting for cluster capacity. The autoscaler may add a node.'
    ),
    SandboxStartupPhase.PROVISIONING_NODE: (
        'The cluster autoscaler is provisioning capacity.'
    ),
    SandboxStartupPhase.SCHEDULING: 'Scheduling the sandbox onto a node.',
    SandboxStartupPhase.PROVISIONING_STORAGE: (
        'Provisioning and attaching sandbox storage.'
    ),
    SandboxStartupPhase.PULLING_IMAGE: 'Pulling the sandbox runtime image.',
    SandboxStartupPhase.STARTING_CONTAINER: 'Starting the sandbox container.',
    SandboxStartupPhase.WAITING_FOR_AGENT_SERVER: (
        'Waiting for the Agent Server to become ready.'
    ),
    SandboxStartupPhase.READY: 'The sandbox is ready.',
    SandboxStartupPhase.FAILED: 'The sandbox failed to start.',
}


@dataclass(frozen=True)
class SandboxStartupObservation:
    progress: SandboxStartupProgress
    progress_key: str
    failure_detail: str | None = None


def _monotonic() -> float:
    return time.monotonic()


def _classify_start_failure(detail: str | None) -> str | None:
    """Translate a raw runtime status_detail into a user-safe message.

    Describes the failure class and likely cause so an owner knows where to
    look, but never echoes the raw detail (it can leak registry hosts, secret
    names, taints, cluster size). Returns None when nothing safe can be said,
    so the caller falls back to a generic message; the raw detail is logged.
    """
    if not detail:
        return None
    if any(m in detail for m in _IMAGE_PULL_MARKERS):
        return (
            'The sandbox image could not be pulled. The image tag may be missing, '
            'the registry credentials may be invalid, or the registry may be '
            'unreachable.'
        )
    if 'CreateContainerConfigError' in detail:
        return (
            'The sandbox failed to start due to a configuration issue (e.g. a '
            'missing secret or config value).'
        )
    if 'CreateContainerError' in detail:
        return (
            'The sandbox container could not be created, possibly due to an '
            'invalid mount, device, or startup command.'
        )
    if any(marker in detail for marker in _STORAGE_MARKERS):
        return (
            'The sandbox storage could not be provisioned or mounted. Check the '
            'workspace storage class and durable volume configuration.'
        )
    resource = next((m for m in _CAPACITY_MARKERS if m in detail), None)
    if resource:
        return (
            f'The system is at capacity right now ({resource.lower()}). '
            'Please try again in a few minutes.'
        )
    if any(m in detail for m in _SCHEDULING_MARKERS):
        return (
            'The sandbox could not be scheduled onto any available node. No node '
            'met its placement requirements (node affinity/selector, taints, or '
            'volume constraints).'
        )
    if 'Insufficient ' in detail:
        return (
            'The sandbox could not be scheduled because a required device or '
            'resource is currently unavailable.'
        )
    return None


def _start_failure_error(sandbox_id: str, detail: str | None) -> SandboxError:
    """Build the user-facing start-failure error: a safe, descriptive message
    tagged with the sandbox id so an owner can find the raw reason in the logs.
    """
    message = _classify_start_failure(detail) or _GENERIC_START_FAILURE
    return SandboxError(f'{message} (reference: {sandbox_id})')


class SandboxService(ABC):
    """Service for accessing sandboxes in which conversations may be run."""

    def get_control_plane_base_url(self) -> str | None:
        """Return a runtime-reachable app-server base URL, when available."""
        return None

    @abstractmethod
    async def search_sandboxes(
        self,
        page_id: str | None = None,
        limit: int = 100,
    ) -> SandboxPage:
        """Search for sandboxes."""

    @abstractmethod
    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a single sandbox. Return None if the sandbox was not found."""

    @abstractmethod
    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get a single sandbox by session API key. Return None if the sandbox was not found."""

    @abstractmethod
    async def get_sandbox_record_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxRecord | None:
        """Get persisted sandbox identity by session API key without querying the runtime.

        Returns only the fields stored in the app server's own database (id and
        owner). Use this for authentication paths that do not need live status,
        exposed URLs, or the plain-text session key — callers avoid a runtime
        API round-trip.

        Return None if no sandbox matches the key.
        """

    async def batch_get_sandboxes(
        self, sandbox_ids: list[str]
    ) -> list[SandboxInfo | None]:
        """Get a batch of sandboxes, returning None for any which were not found."""
        results = await asyncio.gather(
            *[self.get_sandbox(sandbox_id) for sandbox_id in sandbox_ids]
        )
        return results

    @abstractmethod
    async def start_sandbox(
        self, sandbox_spec_id: str | None = None, sandbox_id: str | None = None
    ) -> SandboxInfo:
        """Begin the process of starting a sandbox.

        Return the info on the new sandbox. If no spec is selected, use the default.
        If sandbox_id is provided, it will be used as the sandbox identifier instead
        of generating a random one.
        """

    @abstractmethod
    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Begin the process of resuming a sandbox.

        Return True if the sandbox exists and is being resumed or is already running.
        Return False if the sandbox did not exist.
        """

    async def wait_for_sandbox_running(
        self,
        sandbox_id: str,
        timeout: int = 120,
        poll_interval: int = 2,
        httpx_client: httpx.AsyncClient | None = None,
        inactivity_timeout: int = 120,
    ) -> SandboxInfo:
        """Wait for a sandbox with progress-aware inactivity and absolute limits."""
        sandbox = None
        async for _ in self.watch_sandbox_startup(
            sandbox_id,
            timeout=timeout,
            inactivity_timeout=inactivity_timeout,
            poll_interval=poll_interval,
            httpx_client=httpx_client,
        ):
            sandbox = await self.get_sandbox(sandbox_id)
        if sandbox is None:
            sandbox = await self.get_sandbox(sandbox_id)
        if sandbox is None:
            raise SandboxError(f'Sandbox not found: {sandbox_id}')
        return sandbox

    async def get_sandbox_startup_observation(
        self, sandbox_id: str
    ) -> SandboxStartupObservation:
        """Return a sanitized startup observation.

        Runtime-specific services should override this to expose authoritative
        scheduling, storage, image, and container phases.
        """
        sandbox = await self.get_sandbox(sandbox_id)
        if sandbox is None:
            raise SandboxError(f'Sandbox not found: {sandbox_id}')
        now = utc_now()
        if sandbox.status == SandboxStatus.RUNNING:
            phase = SandboxStartupPhase.READY
            status = SandboxStartupProgressStatus.READY
            reason = 'SANDBOX_RUNNING'
        elif sandbox.status == SandboxStatus.ERROR:
            phase = SandboxStartupPhase.FAILED
            status = SandboxStartupProgressStatus.FAILED
            reason = 'SANDBOX_ERROR'
        else:
            phase = SandboxStartupPhase.WAITING_FOR_AGENT_SERVER
            status = SandboxStartupProgressStatus.PROGRESSING
            reason = 'SANDBOX_STARTING'
        return SandboxStartupObservation(
            progress=SandboxStartupProgress(
                phase=phase,
                status=status,
                message=_STARTUP_PHASE_MESSAGES[phase],
                reason=reason,
                started_at=now,
                updated_at=now,
                phase_started_at=now,
                elapsed_seconds=0,
                phase_elapsed_seconds=0,
                inactivity_seconds=0,
            ),
            progress_key=f'{phase.value}:{sandbox.status_detail or ""}',
            failure_detail=sandbox.status_detail,
        )

    async def watch_sandbox_startup(
        self,
        sandbox_id: str,
        timeout: int = 120,
        inactivity_timeout: int = 120,
        poll_interval: int = 2,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> AsyncGenerator[SandboxStartupProgress, None]:
        """Yield meaningful startup progress until ready or a safe terminal error.

        ``timeout`` is the bounded absolute safety ceiling. The inactivity
        deadline refreshes only when the runtime's progress key changes.
        """
        started_monotonic = _monotonic()
        last_progress_monotonic = started_monotonic
        started_at = utc_now()
        phase_started_at = started_at
        phase_started_monotonic = started_monotonic
        previous_key: str | None = None
        previous_phase: SandboxStartupPhase | None = None

        while True:
            observation = await self.get_sandbox_startup_observation(sandbox_id)
            progress = observation.progress
            progress_key = observation.progress_key
            failure_detail = observation.failure_detail
            sandbox = await self.get_sandbox(sandbox_id)
            if sandbox is None:
                raise SandboxError(f'Sandbox not found: {sandbox_id}')

            if (
                progress.status == SandboxStartupProgressStatus.READY
                and httpx_client
                and sandbox.exposed_urls
                and not await self._check_agent_server_alive(sandbox, httpx_client)
            ):
                progress = progress.model_copy(
                    update={
                        'phase': SandboxStartupPhase.WAITING_FOR_AGENT_SERVER,
                        'status': SandboxStartupProgressStatus.PROGRESSING,
                        'message': _STARTUP_PHASE_MESSAGES[
                            SandboxStartupPhase.WAITING_FOR_AGENT_SERVER
                        ],
                        'reason': 'AGENT_SERVER_NOT_READY',
                    }
                )
                progress_key = 'WAITING_FOR_AGENT_SERVER'

            now_monotonic = _monotonic()
            now = utc_now()
            progressed = progress_key != previous_key
            if progressed:
                last_progress_monotonic = now_monotonic
                previous_key = progress_key
                if progress.phase != previous_phase:
                    phase_started_at = now
                    phase_started_monotonic = now_monotonic
                    previous_phase = progress.phase
            progress = progress.model_copy(
                update={
                    'started_at': started_at,
                    'updated_at': now,
                    'phase_started_at': phase_started_at,
                    'elapsed_seconds': int(now_monotonic - started_monotonic),
                    'phase_elapsed_seconds': int(
                        now_monotonic - phase_started_monotonic
                    ),
                    'inactivity_seconds': int(now_monotonic - last_progress_monotonic),
                }
            )

            if progress.status == SandboxStartupProgressStatus.FAILED:
                _logger.warning(
                    'Sandbox %s entered terminal startup phase; detail=%r',
                    sandbox_id,
                    failure_detail,
                )
                yield progress
                raise _start_failure_error(sandbox_id, failure_detail)

            if progress.status == SandboxStartupProgressStatus.READY:
                yield progress
                return

            elapsed = now_monotonic - started_monotonic
            inactive = now_monotonic - last_progress_monotonic
            if elapsed >= timeout:
                failure = SandboxStartupProgress(
                    **progress.model_dump(
                        exclude={
                            'phase',
                            'status',
                            'message',
                            'reason',
                            'phase_started_at',
                            'phase_elapsed_seconds',
                        }
                    ),
                    phase=SandboxStartupPhase.FAILED,
                    status=SandboxStartupProgressStatus.FAILED,
                    message=(
                        f'The sandbox did not become ready within the {timeout}s '
                        f'safety limit. (reference: {sandbox_id})'
                    ),
                    reason='STARTUP_ABSOLUTE_TIMEOUT',
                    phase_started_at=now,
                    phase_elapsed_seconds=0,
                )
                yield failure
                raise SandboxStartupTimeoutError(failure.message)
            if inactive >= inactivity_timeout:
                failure = SandboxStartupProgress(
                    **progress.model_dump(
                        exclude={
                            'phase',
                            'status',
                            'message',
                            'reason',
                            'phase_started_at',
                            'phase_elapsed_seconds',
                        }
                    ),
                    phase=SandboxStartupPhase.FAILED,
                    status=SandboxStartupProgressStatus.FAILED,
                    message=(
                        f'No verifiable sandbox startup progress occurred for '
                        f'{inactivity_timeout}s. (reference: {sandbox_id})'
                    ),
                    reason='STARTUP_INACTIVITY_TIMEOUT',
                    phase_started_at=now,
                    phase_elapsed_seconds=0,
                )
                yield failure
                raise SandboxStartupTimeoutError(failure.message)

            if progressed:
                yield progress
            await asyncio.sleep(poll_interval)

    async def _check_agent_server_alive(
        self, sandbox: SandboxInfo, httpx_client: httpx.AsyncClient
    ) -> bool:
        """Check if the agent server is responding to health checks.

        Args:
            sandbox: The sandbox info containing exposed URLs
            httpx_client: HTTP client to make the health check request

        Returns:
            True if agent server is alive, False otherwise
        """
        url = None
        try:
            agent_server_url = self._get_agent_server_url(sandbox)
            url = f'{agent_server_url.rstrip("/")}/alive'
            if sandbox.session_api_key:
                response = await httpx_client.get(
                    url,
                    headers={'X-Session-API-Key': sandbox.session_api_key},
                    timeout=5.0,
                )
            else:
                response = await httpx_client.get(url, timeout=5.0)
            return response.is_success
        except Exception as exc:
            _logger.debug(
                f'Agent server health check failed for sandbox {sandbox.id}'
                f'{f" at {url}" if url else ""}: {exc}'
            )
            return False

    def _get_agent_server_url(self, sandbox: SandboxInfo) -> str:
        """Get agent server URL from sandbox exposed URLs.

        Args:
            sandbox: The sandbox info containing exposed URLs

        Returns:
            The agent server URL

        Raises:
            SandboxError: If no agent server URL is found
        """
        if not sandbox.exposed_urls:
            raise SandboxError(f'No exposed URLs for sandbox: {sandbox.id}')

        for exposed_url in sandbox.exposed_urls:
            if exposed_url.name == AGENT_SERVER:
                return replace_localhost_hostname_for_docker(exposed_url.url)

        raise SandboxError(f'No agent server URL found for sandbox: {sandbox.id}')

    @abstractmethod
    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Begin the process of pausing a sandbox.

        Return True if the sandbox exists and is being paused or is already paused.
        Return False if the sandbox did not exist.
        """

    @abstractmethod
    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Begin the process of deleting a sandbox (which may involve stopping it).

        Return False if the sandbox did not exist. Purely sandbox-scoped (stop the
        runtime, delete the record); workspace capture is a separate
        conversation-scoped step (``archive_conversation_workspace``) the
        conversation-delete finalizer runs before the sandbox is torn down.
        """

    async def archive_conversation_workspace(
        self,
        sandbox_id: str,
        conversation_id: str | None = None,
        workspace_path: str | None = None,
    ) -> bool:
        """Archive one conversation's workspace; return whether delete may proceed.

        Default no-op (returns True) for backends that do not archive; overridden
        by RemoteSandboxService. The conversation-delete finalizer calls this
        before ``delete_sandbox`` so the workspace is captured while the runtime is
        still up. ``workspace_path`` is the path pinned at creation. Returns False
        only when archiving is REQUIRED and failed, so the caller leaves the
        sandbox up for a later (idle-reap) capture.
        """
        return True

    async def pause_old_sandboxes(self, max_num_sandboxes: int) -> list[str]:
        """Pause the oldest sandboxes if there are more than max_num_sandboxes running.
        In a multi user environment, this will pause sandboxes only for the current user.

        Args:
            max_num_sandboxes: Maximum number of sandboxes to keep running

        Returns:
            List of sandbox IDs that were paused
        """
        if max_num_sandboxes <= 0:
            raise ValueError('max_num_sandboxes must be greater than 0')

        # Get all running sandboxes (iterate through all pages)
        running_sandboxes = []
        async for sandbox in page_iterator(self.search_sandboxes, limit=100):
            if sandbox.status == SandboxStatus.RUNNING:
                running_sandboxes.append(sandbox)

        # If we're within the limit, no cleanup needed
        if len(running_sandboxes) <= max_num_sandboxes:
            return []

        # Sort by creation time (oldest first)
        running_sandboxes.sort(key=lambda x: x.created_at)

        # Determine how many to pause
        num_to_pause = len(running_sandboxes) - max_num_sandboxes
        sandboxes_to_pause = running_sandboxes[:num_to_pause]

        # Stop the oldest sandboxes
        paused_sandbox_ids = []
        for sandbox in sandboxes_to_pause:
            try:
                success = await self.pause_sandbox(sandbox.id)
                if success:
                    paused_sandbox_ids.append(sandbox.id)
            except Exception:
                # Continue trying to pause other sandboxes even if one fails
                pass

        return paused_sandbox_ids


class SandboxServiceInjector(DiscriminatedUnionMixin, Injector[SandboxService], ABC):
    pass
