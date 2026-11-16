from unittest.mock import patch

import pytest

from openhands.app_server.server_config.server_config import ServerConfig


def test_standalone_uses_existing_file_backed_implementations():
    with patch.dict('os.environ', {}, clear=True):
        config = ServerConfig()
        config.verify_config()

    assert config.user_auth_class.endswith('DefaultUserAuth')
    assert config.settings_store_class.endswith('FileSettingsStore')
    assert config.secret_store_class.endswith('FileSecretsStore')


def test_trusted_identity_selects_open_source_hosted_implementations():
    with patch.dict(
        'os.environ',
        {
            'OH_TRUSTED_IDENTITY_ASSERTION_SECRET': 'secret',
            'OH_TRUSTED_IDENTITY_ASSERTION_ISSUER': 'issuer',
            'OH_TRUSTED_IDENTITY_ASSERTION_AUDIENCE': 'audience',
            'JWT_SECRET': 'storage-secret',
        },
        clear=True,
    ):
        config = ServerConfig()
        config.verify_config()

    assert config.user_auth_class.endswith('TrustedIdentityUserAuth')
    assert config.settings_store_class.endswith('SQLSettingsStore')
    assert config.secret_store_class.endswith('SQLSecretsStore')


def test_trusted_identity_requires_issuer_audience_and_storage_secret():
    with patch.dict(
        'os.environ',
        {'OH_TRUSTED_IDENTITY_ASSERTION_SECRET': 'secret'},
        clear=True,
    ):
        with pytest.raises(ValueError, match='ISSUER.*AUDIENCE.*JWT_SECRET'):
            ServerConfig().verify_config()
