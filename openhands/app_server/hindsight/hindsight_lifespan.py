import asyncio
import sys
from contextlib import asynccontextmanager

import httpx

from openhands.app_server.config import get_global_config
from openhands.app_server.hindsight.hindsight_config import (
    load_hindsight_scope_bindings,
)
from openhands.app_server.hindsight.hindsight_service import (
    HindsightBroker,
    HindsightBrokerSettings,
    run_hindsight_outbox_worker,
)


@asynccontextmanager
async def hindsight_lifespan(_app):
    config = get_global_config()
    if not config.hindsight_enabled:
        yield
        return
    if config.hindsight_linux_only and not sys.platform.startswith('linux'):
        yield
        return
    if not config.hindsight_base_url:
        raise RuntimeError('Hindsight is enabled without a base URL')
    if not config.hindsight_deployment_id:
        raise RuntimeError('Hindsight is enabled without a deployment ID')
    if config.hindsight_api_key_file is None:
        raise RuntimeError('Hindsight is enabled without an API key file')
    if config.hindsight_bank_secret_file is None:
        raise RuntimeError('Hindsight is enabled without a bank secret file')
    client = httpx.AsyncClient(timeout=config.hindsight_request_timeout_seconds)
    settings = HindsightBrokerSettings(
        base_url=config.hindsight_base_url,
        api_key_file=config.hindsight_api_key_file,
        bank_secret_file=config.hindsight_bank_secret_file,
        fail_open=config.hindsight_fail_open,
        max_outbox_pending=config.hindsight_max_outbox_pending,
        max_attempts=config.hindsight_max_attempts,
        request_timeout_seconds=config.hindsight_request_timeout_seconds,
    )
    try:
        settings.get_api_key()
        settings.get_bank_secret()
        if config.hindsight_scope_bindings_json_file is not None:
            load_hindsight_scope_bindings(config.hindsight_scope_bindings_json_file)
    except (OSError, ValueError) as exc:
        await client.aclose()
        raise RuntimeError('Hindsight secret files are unavailable') from exc
    broker = HindsightBroker(
        settings=settings,
        async_session_maker=await config.db_session.get_async_session_maker(),
        http_client=client,
    )
    stop = asyncio.Event()
    worker = asyncio.create_task(run_hindsight_outbox_worker(broker, stop))
    try:
        yield
    finally:
        stop.set()
        await worker
        await client.aclose()
