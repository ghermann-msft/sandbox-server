"""Tests for sandbox_spec_service helpers.

Covers ``get_agent_server_image`` (derived from the installed
``openhands-agent-server`` package, with auto-correction of stale canonical
tags for self-hosted installs) and ``is_custom_sandbox_spec`` (which compares
a sandbox spec id against the bundled default).
"""

import importlib.metadata
import logging
from unittest.mock import AsyncMock, patch

import pytest

from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxOperatingSystem,
    SandboxResourceClass,
    SandboxSpecInfo,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    _bundled_agent_server_version,
    get_agent_server_image,
    is_custom_sandbox_spec,
    resolve_sandbox_spec,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """All the @cache'd helpers in this module need their caches cleared around
    every test so we don't leak env-var or mocked-version state between cases.
    (_bundled_default_image isn't @cache'd; it just calls _bundled_agent_server_version.)"""
    get_agent_server_image.cache_clear()
    _bundled_agent_server_version.cache_clear()
    yield
    get_agent_server_image.cache_clear()
    _bundled_agent_server_version.cache_clear()


def test_get_agent_server_image_derived_from_package_version():
    """The URL must be built from the installed openhands-agent-server version,
    not a hand-maintained constant — that's the whole point of removing the
    drift-prone AGENT_SERVER_IMAGE string."""
    fake_version = '9.9.9'
    with patch.object(
        importlib.metadata,
        'version',
        return_value=fake_version,
    ):
        assert get_agent_server_image() == (
            f'ghcr.io/openhands/agent-server:{fake_version}-python'
        )


def test_get_agent_server_image_returns_consistent_value_within_process():
    """``@cache`` should make repeat calls return the same object without
    re-reading importlib.metadata or re-checking env vars."""
    with patch.object(
        importlib.metadata,
        'version',
        return_value='1.0.0',
    ) as version_mock:
        first = get_agent_server_image()
        # Call many times — version_mock should only be hit once thanks to @cache.
        for _ in range(5):
            assert get_agent_server_image() is first
        assert version_mock.call_count == 1


def test_get_agent_server_image_honours_custom_repository():
    """Self-hosted customers can pin to their own image repository via
    AGENT_SERVER_IMAGE_REPOSITORY; the result must be honoured verbatim,
    including a non-default tag."""
    fake_version = '1.0.0'
    with patch.object(importlib.metadata, 'version', return_value=fake_version):
        with patch.dict(
            'os.environ',
            {
                'AGENT_SERVER_IMAGE_REPOSITORY': 'example.com/agent-server',
                'AGENT_SERVER_IMAGE_TAG': '9.9.9-python',
            },
            clear=False,
        ):
            assert get_agent_server_image() == 'example.com/agent-server:9.9.9-python'


def test_get_agent_server_image_honours_immutable_full_reference():
    image = 'halo.azurecr.io/openhands/agent-server@sha256:abc123'
    with patch.dict(
        'os.environ',
        {
            'AGENT_SERVER_IMAGE': image,
            'AGENT_SERVER_IMAGE_REPOSITORY': 'ignored.example.com/agent-server',
            'AGENT_SERVER_IMAGE_TAG': 'ignored',
        },
        clear=False,
    ):
        assert get_agent_server_image() == image


def test_get_agent_server_image_auto_corrects_stale_canonical_tag():
    """Regression test for the staging 500.

    A canonical-repo + stale ``-python`` tag (the exact staging failure mode)
    must be silently re-pinned to the SDK-matching tag, so self-hosted
    installs keep working when they ship a stale AGENT_SERVER_IMAGE_TAG.
    """
    fake_version = '9.9.9'
    with patch.object(importlib.metadata, 'version', return_value=fake_version):
        with patch.dict(
            'os.environ',
            {
                'AGENT_SERVER_IMAGE_REPOSITORY': 'ghcr.io/openhands/agent-server',
                'AGENT_SERVER_IMAGE_TAG': '1.31.1-python',
            },
            clear=False,
        ):
            assert get_agent_server_image() == (
                f'ghcr.io/openhands/agent-server:{fake_version}-python'
            )


def test_get_agent_server_image_passes_through_canonical_tag_without_suffix():
    """Tags without a ``-`` separator (e.g. ``1.32.0``, ``nightly``) must not
    be silently rewritten by the auto-correct heuristic — that would lose
    legitimate canonical tags. The post-hoc 500-hint will still flag any
    actual SDK mismatch downstream."""
    fake_version = '9.9.9'
    with patch.object(importlib.metadata, 'version', return_value=fake_version):
        with patch.dict(
            'os.environ',
            {
                'AGENT_SERVER_IMAGE_REPOSITORY': 'ghcr.io/openhands/agent-server',
                'AGENT_SERVER_IMAGE_TAG': 'nightly',
            },
            clear=False,
        ):
            assert get_agent_server_image() == (
                'ghcr.io/openhands/agent-server:nightly'
            )


def test_get_agent_server_image_passes_through_canonical_repo_with_matching_tag():
    """Canonical repo + already-matching tag must be a no-op (no warning, no
    rewrite) — the most common case for fresh installs."""
    fake_version = '1.2.3'
    with patch.object(importlib.metadata, 'version', return_value=fake_version):
        with patch.dict(
            'os.environ',
            {
                'AGENT_SERVER_IMAGE_REPOSITORY': 'ghcr.io/openhands/agent-server',
                'AGENT_SERVER_IMAGE_TAG': f'{fake_version}-python',
            },
            clear=False,
        ):
            assert get_agent_server_image() == (
                f'ghcr.io/openhands/agent-server:{fake_version}-python'
            )


def test_is_custom_sandbox_spec_false_for_bundled_default():
    """A spec id equal to the bundled default is, by definition, not custom."""
    with patch.object(importlib.metadata, 'version', return_value='1.0.0'):
        bundled = get_agent_server_image()
        assert is_custom_sandbox_spec(bundled) is False


def test_is_custom_sandbox_spec_true_for_runtime_api_image():
    """A spec id from runtime-api (any non-default image) must be flagged custom."""
    with patch.object(importlib.metadata, 'version', return_value='1.0.0'):
        assert is_custom_sandbox_spec('ghcr.io/some/custom:0.0.1') is True


def test_is_custom_sandbox_spec_true_for_self_hosted_custom_repo():
    """A self-hosted customer who set AGENT_SERVER_IMAGE_REPOSITORY to a
    custom repo still has a non-default image; the post-hoc 500-hint must
    fire for them when their image's SDK doesn't match."""
    fake_version = '1.0.0'
    with patch.object(importlib.metadata, 'version', return_value=fake_version):
        with patch.dict(
            'os.environ',
            {
                'AGENT_SERVER_IMAGE_REPOSITORY': 'example.com/agent-server',
                'AGENT_SERVER_IMAGE_TAG': '9.9.9-python',
            },
            clear=False,
        ):
            assert (
                is_custom_sandbox_spec('example.com/agent-server:9.9.9-python') is True
            )


def test_get_agent_server_image_propagates_package_not_found():
    """openhands-agent-server is a hard runtime dependency; a missing install
    must surface as PackageNotFoundError at first call, not silently degrade."""
    with patch.object(
        importlib.metadata,
        'version',
        side_effect=importlib.metadata.PackageNotFoundError('openhands-agent-server'),
    ):
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            get_agent_server_image()


def test_legacy_sandbox_spec_defaults_to_linux_standard_profile():
    spec = SandboxSpecInfo(
        id='legacy',
        command=None,
    )

    assert spec.operating_system == SandboxOperatingSystem.LINUX
    assert spec.resource_class == SandboxResourceClass.STANDARD
    assert spec.cpu_cores is None
    assert spec.memory_gib is None


def test_available_windows_profile_requires_digest_pinned_image():
    with pytest.raises(ValueError, match='digest-pinned'):
        SandboxSpecInfo(
            id='windows',
            command=None,
            operating_system=SandboxOperatingSystem.WINDOWS,
            platform='windows/amd64',
            image='ghcr.io/openhands/agent-server:windows-ltsc2025-amd64',
        )


async def test_explicit_unavailable_profile_has_deterministic_error():
    service = AsyncMock()
    unavailable = SandboxSpecInfo(
        id='windows-server-2025-amd64',
        command=None,
        availability='unavailable',
        availability_reason='digest-pinned image not configured',
    )
    service.get_sandbox_spec.side_effect = [None, unavailable]

    with pytest.raises(SandboxError, match='digest-pinned image not configured'):
        await resolve_sandbox_spec(
            unavailable.id,
            None,
            service,
            logging.getLogger(__name__),
        )
