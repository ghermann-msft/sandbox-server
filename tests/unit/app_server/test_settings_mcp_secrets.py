from openhands.app_server.settings.settings_models import Settings
from openhands.sdk.mcp.config import dump_mcp_config
from openhands.sdk.utils.pydantic_secrets import REDACTED_SECRET_VALUE


def _update_mcp(settings: Settings, servers: dict) -> None:
    settings.update(
        {
            'agent_settings_diff': {
                'mcp_config': {'mcpServers': servers},
            }
        }
    )


def _dump_mcp(settings: Settings) -> dict:
    return dump_mcp_config(
        settings.agent_settings.mcp_config,
        context={'expose_secrets': 'plaintext'},
    )


def test_same_key_command_change_preserves_redacted_and_omitted_p4_env():
    settings = Settings()
    credentials = {
        'P4PORT': 'ssl:perforce.example.com:1666',
        'P4USER': 'test-user',
        'P4PASSWD': 'test-password',
    }
    _update_mcp(
        settings,
        {
            'MCC': {
                'command': '/usr/local/bin/p4-mcp-server',
                'env': credentials,
            },
            'Miramonte': {
                'command': '/usr/local/bin/p4-mcp-server',
                'args': ['--workspace', 'miramonte'],
                'env': credentials,
            },
        },
    )

    _update_mcp(
        settings,
        {
            'MCC': {
                'command': 'p4-mcp-server',
                'env': {key: REDACTED_SECRET_VALUE for key in credentials},
            },
            'Miramonte': {
                'command': 'p4-mcp-server',
                'args': ['--workspace', 'miramonte'],
            },
        },
    )

    dumped = _dump_mcp(settings)
    assert dumped['MCC']['command'] == 'p4-mcp-server'
    assert dumped['MCC']['env'] == credentials
    assert dumped['Miramonte']['command'] == 'p4-mcp-server'
    assert dumped['Miramonte']['env'] == credentials


def test_cross_key_different_endpoint_does_not_transplant_p4_env():
    settings = Settings()
    credentials = {
        'P4PORT': 'ssl:perforce.example.com:1666',
        'P4USER': 'test-user',
        'P4PASSWD': 'test-password',
    }
    _update_mcp(
        settings,
        {
            'MCC': {
                'command': '/usr/local/bin/p4-mcp-server',
                'env': credentials,
            }
        },
    )

    _update_mcp(
        settings,
        {
            'renamed': {
                'command': 'unrelated-mcp-server',
                'env': {key: REDACTED_SECRET_VALUE for key in credentials},
            }
        },
    )

    dumped = _dump_mcp(settings)
    assert 'MCC' not in dumped
    assert dumped['renamed'].get('env', {}) == {}
