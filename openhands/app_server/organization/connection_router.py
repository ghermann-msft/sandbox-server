from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.config import depends_jwt_service, depends_user_context
from openhands.app_server.organization.connection_models import (
    MCPConnectionUpsert,
    OrganizationConnectionKind,
    OrganizationConnectionList,
    OrganizationConnectionStatus,
)
from openhands.app_server.organization.connection_service import (
    OrganizationConnectionService,
)
from openhands.app_server.organization.organization_models import (
    OrganizationPermission,
)
from openhands.app_server.organization.organization_service import OrganizationService
from openhands.app_server.services.db_session import depends_db_session
from openhands.app_server.services.jwt_service import JwtService
from openhands.app_server.user.user_context import UserContext

router = APIRouter(
    prefix='/api/organizations/{organization_id}/connections',
    tags=['organization-connections'],
)
db_session_dependency = depends_db_session()
user_context_dependency = depends_user_context()
jwt_service_dependency = depends_jwt_service()
requested_org_id_header = Header(default=None, alias='X-OpenHands-Organization-ID')


def _service(
    db_session: AsyncSession, user_context: UserContext, jwt_service: JwtService
) -> OrganizationConnectionService:
    return OrganizationConnectionService(db_session, user_context, jwt_service)


async def _validate_selected_organization(
    organization_id: UUID,
    requested_org_id: UUID | None,
    db_session: AsyncSession,
    user_context: UserContext,
) -> None:
    selected = await OrganizationService(
        db_session, user_context
    ).resolve_organization_id(requested_org_id, OrganizationPermission.ORG_VIEW)
    if selected != organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Selected organization does not match requested organization',
        )


@router.get('')
async def list_connections(
    organization_id: UUID,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> OrganizationConnectionList:
    await _validate_selected_organization(
        organization_id, requested_org_id, db_session, user_context
    )
    return await _service(db_session, user_context, jwt_service).list(organization_id)


@router.put('/{kind}')
async def put_connection(
    organization_id: UUID,
    kind: OrganizationConnectionKind,
    incoming: MCPConnectionUpsert,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> OrganizationConnectionStatus:
    await _validate_selected_organization(
        organization_id, requested_org_id, db_session, user_context
    )
    if kind == OrganizationConnectionKind.GITHUB:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail='GitHub connections must be established through OAuth',
        )
    row = await _service(db_session, user_context, jwt_service).store_mcp(
        organization_id, kind, incoming
    )
    return OrganizationConnectionStatus(
        kind=kind,
        connected=True,
        editable=True,
        scope='shared',
        display_name=row.public_metadata.get('display_name'),
        endpoint=row.public_metadata.get('endpoint'),
        transport=row.public_metadata.get('transport'),
        migrated_from_user=bool(row.public_metadata.get('migrated_from_user')),
    )


@router.delete('/{kind}', status_code=204)
async def delete_connection(
    organization_id: UUID,
    kind: OrganizationConnectionKind,
    requested_org_id: UUID | None = requested_org_id_header,
    db_session: AsyncSession = db_session_dependency,
    user_context: UserContext = user_context_dependency,
    jwt_service: JwtService = jwt_service_dependency,
) -> None:
    await _validate_selected_organization(
        organization_id, requested_org_id, db_session, user_context
    )
    await _service(db_session, user_context, jwt_service).delete(organization_id, kind)
