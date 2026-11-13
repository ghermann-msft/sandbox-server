from pathlib import Path

DOCKERFILE = Path(__file__).parents[2] / 'containers' / 'app' / 'Dockerfile'


def test_software_agent_sdk_override_is_opt_in_and_uses_one_checkout():
    dockerfile = DOCKERFILE.read_text()

    assert (
        'ARG SOFTWARE_AGENT_SDK_REPOSITORY='
        'https://github.com/OpenHands/software-agent-sdk.git'
    ) in dockerfile
    assert 'ARG SOFTWARE_AGENT_SDK_REF=' in dockerfile
    assert 'if [ -n "${SOFTWARE_AGENT_SDK_REF}" ]; then' in dockerfile
    assert (
        'git -C /tmp/software-agent-sdk fetch --depth 1 origin '
        '"${SOFTWARE_AGENT_SDK_REF}"'
    ) in dockerfile
    assert 'cd /tmp/software-agent-sdk' in dockerfile
    assert 'uv export --frozen --no-dev' in dockerfile
    assert '--package openhands-agent-server --no-editable --no-hashes' in dockerfile
    assert '--reinstall-package fastmcp --reinstall-package fastmcp-slim' in dockerfile
    assert '/app/.venv/bin/python -c' in dockerfile
    assert 'poetry run python -c' not in dockerfile
    assert 'from fastmcp import Client' in dockerfile
    assert 'import openhands.sdk.mcp.client' in dockerfile
    assert 'import openhands.agent_server.models' in dockerfile


def test_runtime_image_merges_and_imports_the_openhands_namespace():
    dockerfile = DOCKERFILE.read_text()

    source_copy = 'COPY --chown=openhands:openhands --chmod=770 openhands openhands'
    namespace_copy = (
        'COPY --chown=openhands:openhands --chmod=770 '
        '--from=backend-builder /tmp/runtime-openhands/ openhands/'
    )
    final_smoke = (
        '"import openhands.app_server.app; import openhands.sdk; '
        'import openhands.agent_server.models"'
    )

    assert 'OPENHANDS_NAMESPACE_DIR=$(/app/.venv/bin/python -c' in dockerfile
    assert 'cp -a "${OPENHANDS_NAMESPACE_DIR}/." /tmp/runtime-openhands/' in dockerfile
    assert 'rm -f /tmp/runtime-openhands/__init__.py' in dockerfile
    assert source_copy in dockerfile
    assert namespace_copy in dockerfile
    assert final_smoke in dockerfile
    assert dockerfile.index(source_copy) < dockerfile.index(namespace_copy)
    assert dockerfile.index(namespace_copy) < dockerfile.index(final_smoke)
