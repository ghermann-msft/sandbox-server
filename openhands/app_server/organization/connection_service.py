from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.integrations.provider import ProviderToken
from openhands.app_server.integrations.service_types import ProviderType
from openhands.app_server.organization.connection_models import (
    MCPConnectionUpsert,
    OrganizationConnectionKind,
    OrganizationConnectionList,
    OrganizationConnectionStatus,
    StoredOrganizationConnection,
)
from openhands.app_server.organization.organization_models import (
    OrganizationKind,
    OrganizationPermission,
    StoredMembership,
    StoredOrganization,
)
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.secrets.secrets_models import Secrets
from openhands.app_server.secrets.sql_secrets_store import StoredUserSecrets
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.settings.settings_models import Settings
from openhands.app_server.settings.sql_settings_store import StoredUserSettings
from openhands.app_server.user.user_context import UserContext
from openhands.sdk.mcp.config import MCPServer

_MANAGE_PERMISSIONS = {
    OrganizationPermission.ORG_MANAGE,
    OrganizationPermission.ORG_MANAGE_MEMBERS,
}
_MCP_NAMES = {
    OrganizationConnectionKind.ATLASSIAN: ('atlassian', 'jira'),
    OrganizationConnectionKind.PERFORCE: ('perforce',),
}


@dataclass
class OrganizationConnectionService:
    db_session: AsyncSession
    user_context: UserContext
    jwt_service: JwtService

    @property
    def organizations(self) -> OrganizationService:
        return OrganizationService(self.db_session, self.user_context)

    async def _organization(
        self,
        organization_id: UUID,
        *,
        manage: bool = False,
    ) -> tuple[StoredOrganization, bool]:
        membership = (
            await self.organizations.require_any_permission(
                organization_id, _MANAGE_PERMISSIONS
            )
            if manage
            else await self.organizations.require_permission(
                organization_id, OrganizationPermission.ORG_VIEW
            )
        )
        organization = await self.db_session.get(StoredOrganization, organization_id)
        if organization is None:
            raise RuntimeError(
                'Organization membership references a missing organization'
            )
        return organization, bool(
            _MANAGE_PERMISSIONS.intersection(membership.permissions)
        )

    def _encrypt(self, payload: dict[str, Any]) -> str:
        return self.jwt_service.encrypt_value(
            json.dumps(payload, sort_keys=True, separators=(',', ':'))
        )

    def _decrypt(self, row: StoredOrganizationConnection) -> dict[str, Any]:
        return json.loads(self.jwt_service.decrypt_value(row.encrypted_payload))

    async def _row(
        self, organization_id: UUID, kind: OrganizationConnectionKind
    ) -> StoredOrganizationConnection | None:
        return await self.db_session.get(
            StoredOrganizationConnection, (organization_id, kind.value)
        )

    async def _store(
        self,
        organization_id: UUID,
        kind: OrganizationConnectionKind,
        payload: dict[str, Any],
        public_metadata: dict[str, Any],
    ) -> StoredOrganizationConnection:
        user = await self.organizations._user()
        row = await self._row(organization_id, kind)
        encrypted_payload = self._encrypt(payload)
        if row is None:
            row = StoredOrganizationConnection(
                organization_id=organization_id,
                kind=kind.value,
                encrypted_payload=encrypted_payload,
                public_metadata=public_metadata,
                created_by_user_id=user.id,
                updated_by_user_id=user.id,
            )
            self.db_session.add(row)
        else:
            row.encrypted_payload = encrypted_payload
            row.public_metadata = public_metadata
            row.updated_by_user_id = user.id
            row.revision += 1
        await self.db_session.flush()
        return row

    async def _maybe_migrate(
        self, organization_id: UUID, kind: OrganizationConnectionKind
    ) -> StoredOrganizationConnection | None:
        existing = await self._row(organization_id, kind)
        if existing is not None:
            return existing

        organization, editable = await self._organization(organization_id)
        if organization.kind != OrganizationKind.SHARED.value or not editable:
            return None

        user = await self.organizations._user()
        shared_org_ids = list(
            (
                await self.db_session.scalars(
                    select(StoredMembership.organization_id)
                    .join(
                        StoredOrganization,
                        StoredOrganization.id == StoredMembership.organization_id,
                    )
                    .where(
                        StoredMembership.user_id == user.id,
                        StoredOrganization.kind == OrganizationKind.SHARED.value,
                    )
                )
            ).all()
        )
        if len(shared_org_ids) != 1 or shared_org_ids[0] != organization_id:
            return None

        user_id = await self.user_context.get_user_id()
        if not user_id:
            return None

        if kind == OrganizationConnectionKind.GITHUB:
            stored = await self.db_session.get(StoredUserSecrets, user_id)
            if stored is None:
                return None
            secrets = Secrets.model_validate_json(
                self.jwt_service.decrypt_value(stored.encrypted_payload)
            )
            token = secrets.provider_tokens.get(ProviderType.GITHUB)
            if token is None or token.token is None:
                return None
            return await self.store_github_token(
                organization_id,
                token,
                migrated_from_user=True,
                require_manage=False,
            )

        stored_settings = await self.db_session.get(StoredUserSettings, user_id)
        if stored_settings is None:
            return None
        settings = Settings.model_validate_json(
            self.jwt_service.decrypt_value(stored_settings.encrypted_payload)
        )
        names = _MCP_NAMES[kind]
        candidates = [
            (name, server)
            for name, server in settings.agent_settings.mcp_config.items()
            if name.lower() in names
        ]
        if len(candidates) != 1:
            return None
        server_name, server = candidates[0]
        return await self.store_mcp(
            organization_id,
            kind,
            MCPConnectionUpsert(server=server, display_name=server_name),
            migrated_from_user=True,
            require_manage=False,
        )

    @staticmethod
    def _server_payload(server: MCPServer) -> dict[str, Any]:
        return server.model_dump(
            mode='json',
            context={'expose_secrets': True},
            exclude_none=True,
        )

    @staticmethod
    def _server_metadata(
        server: MCPServer,
        display_name: str | None,
        migrated_from_user: bool,
    ) -> dict[str, Any]:
        dumped = server.model_dump(mode='json', exclude_none=True)
        endpoint = dumped.get('command')
        url = dumped.get('url')
        if isinstance(url, str):
            parsed = urlsplit(url)
            host = parsed.hostname
            if host:
                endpoint = f'{parsed.scheme}://{host}'
                if parsed.port is not None:
                    endpoint += f':{parsed.port}'
        return {
            'display_name': display_name,
            'endpoint': endpoint,
            'transport': dumped.get('transport'),
            'migrated_from_user': migrated_from_user,
        }

    async def store_mcp(
        self,
        organization_id: UUID,
        kind: OrganizationConnectionKind,
        incoming: MCPConnectionUpsert,
        *,
        migrated_from_user: bool = False,
        require_manage: bool = True,
    ) -> StoredOrganizationConnection:
        if kind not in (
            OrganizationConnectionKind.ATLASSIAN,
            OrganizationConnectionKind.PERFORCE,
        ):
            raise ValueError('Only Atlassian and Perforce use MCP connection payloads')
        await self._organization(organization_id, manage=require_manage)
        return await self._store(
            organization_id,
            kind,
            {'server': self._server_payload(incoming.server)},
            self._server_metadata(
                incoming.server, incoming.display_name, migrated_from_user
            ),
        )

    async def store_github_token(
        self,
        organization_id: UUID,
        token: ProviderToken,
        *,
        migrated_from_user: bool = False,
        require_manage: bool = True,
    ) -> StoredOrganizationConnection:
        await self._organization(organization_id, manage=require_manage)
        if token.token is None:
            raise ValueError('GitHub OAuth did not return a token')
        return await self._store(
            organization_id,
            OrganizationConnectionKind.GITHUB,
            {
                'token': token.token.get_secret_value(),
                'user_id': token.user_id,
                'host': token.host,
            },
            {
                'display_name': token.user_id,
                'endpoint': token.host,
                'migrated_from_user': migrated_from_user,
            },
        )

    async def delete(
        self, organization_id: UUID, kind: OrganizationConnectionKind
    ) -> None:
        await self._organization(organization_id, manage=True)
        row = await self._row(organization_id, kind)
        if row is not None:
            await self.db_session.delete(row)
            await self.db_session.flush()

    async def github_token(self, organization_id: UUID) -> ProviderToken | None:
        organization, _ = await self._organization(organization_id)
        if organization.kind != OrganizationKind.SHARED.value:
            return None
        row = await self._row(organization_id, OrganizationConnectionKind.GITHUB)
        if row is None:
            row = await self._maybe_migrate(
                organization_id, OrganizationConnectionKind.GITHUB
            )
        if row is None:
            return None
        payload = self._decrypt(row)
        return ProviderToken(
            token=SecretStr(payload['token']),
            user_id=payload.get('user_id'),
            host=payload.get('host'),
        )

    async def mcp_servers(self, organization_id: UUID) -> dict[str, MCPServer]:
        organization, _ = await self._organization(organization_id)
        if organization.kind != OrganizationKind.SHARED.value:
            return {}
        result: dict[str, MCPServer] = {}
        for kind in (
            OrganizationConnectionKind.ATLASSIAN,
            OrganizationConnectionKind.PERFORCE,
        ):
            row = await self._row(organization_id, kind)
            if row is None:
                row = await self._maybe_migrate(organization_id, kind)
            if row is not None:
                result[kind.value] = MCPServer.model_validate(
                    self._decrypt(row)['server']
                )
        return result

    async def list(self, organization_id: UUID) -> OrganizationConnectionList:
        organization, editable = await self._organization(organization_id)
        if organization.kind != OrganizationKind.SHARED.value:
            return OrganizationConnectionList(organization_id=organization_id, items=[])
        items = []
        for kind in OrganizationConnectionKind:
            row = await self._row(organization_id, kind)
            if row is None:
                row = await self._maybe_migrate(organization_id, kind)
            metadata = row.public_metadata if row is not None else {}
            items.append(
                OrganizationConnectionStatus(
                    kind=kind,
                    connected=row is not None,
                    editable=editable,
                    scope=OrganizationKind.SHARED.value,
                    display_name=metadata.get('display_name'),
                    endpoint=metadata.get('endpoint'),
                    transport=metadata.get('transport'),
                    migrated_from_user=bool(metadata.get('migrated_from_user')),
                )
            )
        return OrganizationConnectionList(organization_id=organization_id, items=items)
