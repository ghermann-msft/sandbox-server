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
    assert '/tmp/software-agent-sdk/openhands-sdk' in dockerfile
    assert '/tmp/software-agent-sdk/openhands-tools' in dockerfile
    assert '/tmp/software-agent-sdk/openhands-agent-server' in dockerfile
    assert 'poetry run python -m pip check' in dockerfile
