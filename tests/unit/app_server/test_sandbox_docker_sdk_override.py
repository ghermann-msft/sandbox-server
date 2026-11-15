from pathlib import Path


def test_sandbox_image_pins_workload_identity_sdk_runtime():
    dockerfile = (
        Path(__file__).parents[3] / 'containers' / 'app' / 'Dockerfile'
    ).read_text()

    assert (
        'SOFTWARE_AGENT_SDK_REF=b1d818a6078691cd765e05adbc60cd5fecb4c671' in dockerfile
    )
    assert (
        'SOFTWARE_AGENT_SDK_REPOSITORY=https://github.com/ghermann-msft/'
        'software-agent-sdk.git' in dockerfile
    )
    assert '#subdirectory=openhands-agent-server' in dockerfile
    assert '#subdirectory=openhands-sdk' in dockerfile
    assert '#subdirectory=openhands-tools' in dockerfile
    assert "auth_type='azure_workload_identity'" in dockerfile
    assert '_default_hosted_settings' in dockerfile
