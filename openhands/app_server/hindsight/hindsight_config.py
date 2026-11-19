import os
from pathlib import Path

from openhands.app_server.hindsight.hindsight_models import HindsightScopeBindings


def get_hindsight_enabled() -> bool:
    return os.getenv('OH_HINDSIGHT_ENABLED', 'false').lower() == 'true'


def get_hindsight_fail_open() -> bool:
    return os.getenv('OH_HINDSIGHT_FAIL_OPEN', 'true').lower() == 'true'


def get_hindsight_linux_only() -> bool:
    return os.getenv('OH_HINDSIGHT_LINUX_ONLY', 'true').lower() == 'true'


def get_hindsight_base_url() -> str | None:
    return os.getenv('OH_HINDSIGHT_BASE_URL')


def get_hindsight_broker_url() -> str | None:
    return os.getenv('OH_HINDSIGHT_BROKER_URL')


def get_hindsight_api_key_file() -> Path | None:
    value = os.getenv('OH_HINDSIGHT_API_KEY_FILE')
    return Path(value) if value else None


def get_hindsight_bank_secret_file() -> Path | None:
    value = os.getenv('OH_HINDSIGHT_BANK_SECRET_FILE')
    return Path(value) if value else None


def get_hindsight_deployment_id() -> str | None:
    return os.getenv('OH_HINDSIGHT_DEPLOYMENT_ID')


def get_hindsight_scope_bindings_json_file() -> Path | None:
    value = os.getenv('OH_HINDSIGHT_SCOPE_BINDINGS_JSON_FILE')
    return Path(value) if value else None


def read_hindsight_secret_file(path: Path) -> str:
    value = path.read_text(encoding='utf-8').rstrip('\r\n')
    if not value or '\n' in value or '\r' in value:
        raise ValueError('Hindsight secret file must contain one non-empty value')
    return value


def load_hindsight_scope_bindings(file_path: Path) -> HindsightScopeBindings:
    return HindsightScopeBindings.model_validate_json(
        file_path.read_text(encoding='utf-8')
    )
