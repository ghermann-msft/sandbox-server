from pathlib import Path


def test_sandbox_image_pins_workload_identity_sdk_runtime():
    dockerfile = (
        Path(__file__).parents[3] / 'containers' / 'app' / 'Dockerfile'
    ).read_text()

    assert (
        'SOFTWARE_AGENT_SDK_REF=cb6974d484cdf9e85d7e1940a43bea3f360686d6' in dockerfile
    )
    assert (
        'SOFTWARE_AGENT_SDK_REPOSITORY=https://github.com/ghermann-msft/'
        'software-agent-sdk.git' in dockerfile
    )
    assert '#subdirectory=openhands-agent-server' in dockerfile
    assert '#subdirectory=openhands-sdk' in dockerfile
    assert '#subdirectory=openhands-tools' in dockerfile
    assert '.venv/bin/pip uninstall -y fastmcp' in dockerfile
    assert 'from fastmcp.mcp_config import MCPConfig' in dockerfile
    assert 'from openhands.app_server.app import app' in dockerfile
    assert "auth_type='azure_workload_identity'" in dockerfile
    assert '_default_hosted_settings' in dockerfile
    assert 'FOUNDRY_WEB_SEARCH_MCP_ENDPOINT=' in dockerfile
    assert 'FOUNDRY_WEB_SEARCH_MCP_AUTH_TYPE=azure_workload_identity' in dockerfile
    assert 'MCPAzureWorkloadIdentityAuthCredential' in dockerfile
    assert 'Settings.model_validate_json' in dockerfile
    assert 'persisted_settings.agent_settings.mcp_config[' in dockerfile
