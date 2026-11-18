from __future__ import annotations

import logging
import os
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from openhands.app_server.app_lifespan.app_lifespan_service import AppLifespanService
from openhands.app_server.organization.organization_service import (
    bootstrap_organization_from_env,
)
from openhands.app_server.sandbox.sandbox_access import StoredSandboxAccess
from openhands.app_server.sandbox.sandbox_lifecycle import active_sandbox_ids

_ALEMBIC_ADVISORY_LOCK_ID = 684_301_179_211_155_017
_logger = logging.getLogger(__name__)


class OssAppLifespanService(AppLifespanService):
    run_alembic_on_startup: bool = True

    async def __aenter__(self):
        if self.run_alembic_on_startup:
            self.run_alembic()
            from openhands.app_server.config import get_global_config

            bootstrap_organization_from_env(
                get_global_config().db_session.get_db_engine()
            )
            try:
                await self.reconcile_sandboxes()
            except Exception:
                _logger.exception(
                    'Sandbox startup reconciliation failed; creation-time '
                    'reconciliation will retry'
                )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        pass

    async def reconcile_sandboxes(self) -> None:
        from openhands.app_server.config import (
            get_db_session,
            get_global_config,
            get_sandbox_spec_service,
        )
        from openhands.app_server.sandbox.kubernetes_sandbox_service import (
            KubernetesSandboxService,
            KubernetesSandboxServiceInjector,
        )
        from openhands.app_server.services.injector import InjectorState

        config = get_global_config()
        injector = config.sandbox
        if not isinstance(injector, KubernetesSandboxServiceInjector):
            return

        state = InjectorState()
        async with (
            get_sandbox_spec_service(state) as sandbox_spec_service,
            get_db_session(state) as db_session,
        ):
            sandbox_service = KubernetesSandboxService(
                sandbox_spec_service=sandbox_spec_service,
                namespace=injector.namespace,
                warm_pool=injector.warm_pool,
                claim_name_prefix=injector.claim_name_prefix,
                sandbox_url_pattern=injector.sandbox_url_pattern,
                webhook_base_url=injector.webhook_base_url,
                exposed_ports=injector.exposed_ports,
                max_num_sandboxes=injector.max_num_sandboxes,
                web_url=config.web_url,
                permitted_cors_origins=config.permitted_cors_origins,
                inject_session_key=injector.inject_session_key,
                shutdown_after_seconds=injector.shutdown_after_seconds,
                validate_profile_infrastructure=(
                    injector.validate_profile_infrastructure
                ),
            )
            deleted_ids = await sandbox_service.reconcile_orphaned_sandboxes(
                await active_sandbox_ids(db_session)
            )
            if deleted_ids:
                await db_session.execute(
                    sa.delete(StoredSandboxAccess).where(
                        StoredSandboxAccess.sandbox_id.in_(deleted_ids)
                    )
                )
                await db_session.commit()

    def run_alembic(self):
        # Run alembic upgrade head to ensure database is up to date
        alembic_dir = Path(__file__).parent / 'alembic'
        alembic_ini = alembic_dir / 'alembic.ini'

        # Create alembic config with absolute paths
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option('script_location', str(alembic_dir))

        from openhands.app_server.config import get_global_config

        engine = get_global_config().db_session.get_db_engine()

        # Change to alembic directory for the command execution
        original_cwd = os.getcwd()
        try:
            os.chdir(str(alembic_dir.parent))
            with engine.connect() as connection:
                is_postgresql = connection.dialect.name == 'postgresql'
                if is_postgresql:
                    connection.execute(
                        sa.text('SELECT pg_advisory_lock(:lock_id)'),
                        {'lock_id': _ALEMBIC_ADVISORY_LOCK_ID},
                    )
                    connection.commit()

                alembic_cfg.attributes['connection'] = connection
                try:
                    command.upgrade(alembic_cfg, 'head')
                finally:
                    alembic_cfg.attributes.pop('connection', None)
                    if is_postgresql:
                        if connection.in_transaction():
                            connection.rollback()
                        connection.execute(
                            sa.text('SELECT pg_advisory_unlock(:lock_id)'),
                            {'lock_id': _ALEMBIC_ADVISORY_LOCK_ID},
                        )
                        connection.commit()
        finally:
            os.chdir(original_cwd)
