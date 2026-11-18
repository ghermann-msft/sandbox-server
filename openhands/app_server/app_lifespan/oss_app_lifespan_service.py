from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from openhands.app_server.app_lifespan.app_lifespan_service import AppLifespanService
from openhands.app_server.organization.organization_service import (
    bootstrap_organization_from_env,
)

_ALEMBIC_ADVISORY_LOCK_ID = 684_301_179_211_155_017


class OssAppLifespanService(AppLifespanService):
    run_alembic_on_startup: bool = True

    async def __aenter__(self):
        if self.run_alembic_on_startup:
            self.run_alembic()
            from openhands.app_server.config import get_global_config

            bootstrap_organization_from_env(
                get_global_config().db_session.get_db_engine()
            )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        pass

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
