import re
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
P4_MCP_VERSION = '2026.4.3126709'
P4_MCP_SHA256 = 'f2ea428cf35acec04e822472b090128ff9e522d07264ab1211d46b05e479a387'
P4_MCP_URL = (
    'https://github.com/perforce/p4mcp-server/releases/download/'
    f'v{P4_MCP_VERSION}/p4-mcp-server-linux.zip'
)


def _agent_server_version() -> str:
    pyproject = tomllib.loads((REPOSITORY_ROOT / 'pyproject.toml').read_text())
    dependency = next(
        dependency
        for dependency in pyproject['project']['dependencies']
        if dependency.startswith('openhands-agent-server==')
    )
    return dependency.removeprefix('openhands-agent-server==')


def test_perforce_mcp_runtime_image_pins_release_and_non_root_user():
    dockerfile = (REPOSITORY_ROOT / 'containers' / 'sandbox' / 'Dockerfile').read_text()
    agent_server_version = _agent_server_version()

    assert (
        f'ARG AGENT_SERVER_IMAGE=ghcr.io/openhands/agent-server:'
        f'{agent_server_version}-python'
    ) in dockerfile
    assert f'ARG P4_MCP_VERSION={P4_MCP_VERSION}' in dockerfile
    assert f'ARG P4_MCP_URL={P4_MCP_URL}' in dockerfile
    assert f'ARG P4_MCP_SHA256={P4_MCP_SHA256}' in dockerfile
    assert (
        'ln -s /opt/perforce/p4mcp-server/p4-mcp-server /usr/local/bin/p4-mcp-server'
    ) in dockerfile
    assert (
        'install -d -o openhands -g openhands /opt/perforce/p4mcp-server/logs'
    ) in dockerfile
    assert re.findall(r'^USER (.+)$', dockerfile, re.MULTILINE)[-1] == 'openhands'


def test_perforce_mcp_runtime_image_tag_is_consistent():
    tag = f'{_agent_server_version()}-python-p4mcp-{P4_MCP_VERSION}'
    image = f'ghcr.io/openhands/agent-server:{tag}'

    compose = (REPOSITORY_ROOT / 'docker-compose.yml').read_text()
    kubernetes = (
        REPOSITORY_ROOT / 'deploy' / 'kubernetes' / 'sandboxtemplate.yaml'
    ).read_text()
    makefile = (REPOSITORY_ROOT / 'Makefile').read_text()

    assert f'AGENT_SERVER_IMAGE=${{AGENT_SERVER_IMAGE:-{image}}}' in compose
    assert f'image: {image}' in kubernetes
    assert f'-t {image} .' in makefile
