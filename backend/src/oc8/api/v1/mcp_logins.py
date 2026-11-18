"""Create/list tool logins (unified connections design, "Storage").

Creating a login makes (or reuses) an organisation `Credential`; for an agent
it becomes the agent's fixed access (`AgentToolAccess`). No connection row is
created per login any more -- the tool has one row, and the run resolves which
login it starts with. The listing still shows the login-pinned rows older
versions created, until the tenant's rows are consolidated."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1.agent_tool_access import (
    authorized_agent,
    connectable_integration,
    save_tool_access,
)
from oc8.auth import Principal
from oc8.authz.permissions import INTEGRATION, MANAGE, VIEW, perm
from oc8.authz.scope import HumanActor, scope_for_principal
from oc8.connections.integrations import Integration
from oc8.connections.tool_connection import tool_connection
from oc8.credentials.enablement import choose_for_department
from oc8.credentials.registry import CredentialTypeNotFound, get_credential_type
from oc8.credentials.service import CredentialNotFound, create_credential, get_credential
from oc8.schemas.dto import McpLoginDTO
from oc8.schemas.requests import CreateMcpLoginRequest, SetAgentToolAccessRequest
from oc8.secrets.keyprovider import SecretStoreUnavailable

router = APIRouter()


def _to_dto(c: m.McpConnection) -> McpLoginDTO:
    return McpLoginDTO(
        id=str(c.id),
        name=c.name,
        credential_id=str(c.credential_id),
        department_id=str(c.department_id) if c.department_id else None,
        connected=c.connected,
        scopes=c.scopes,
        health=c.health or {},
    )


@router.post(
    "/mcp/logins",
    response_model=McpLoginDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def create_login(
    body: CreateMcpLoginRequest, request: Request, db: DbSession, principal: CurrentPrincipal
) -> McpLoginDTO:
    """Create (or reuse) an organisation login for the tool `name`. With an
    `agent_id` the login becomes that agent's fixed access for the tool --
    the same row the agent's Tools tab writes; otherwise, with a
    `department_id`, it is enabled for that department. Never a further
    connection row: the tool keeps its one row, and the run resolves the
    login."""
    try:
        await get_credential_type(db, tenant_id=principal.tenant_id, name=body.credential_type)
    except CredentialTypeNotFound as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown credential type: {exc}") from exc
    dept: m.Department | None = None
    if body.department_id is not None:
        dept = await db.get(m.Department, body.department_id)
        if dept is None or dept.deleted_at is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown department")
    agent: m.Agent | None = None
    actor: HumanActor | None = None
    integ: Integration | None = None
    if body.agent_id is not None:
        # The same door as `PUT /agents/{id}/tool-access/{key}`: the right to
        # change THIS agent, checked before anything is written.
        actor = await _human_actor(db, principal)
        agent = await authorized_agent(request, db, actor, body.agent_id)
        integ = await connectable_integration(db, body.name)
        if (
            integ is None
            or integ.kind != "credential"
            or integ.credential_type != body.credential_type
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{body.name!r} is no tool that takes a {body.credential_type!r} login",
            )
    if body.credential_id is not None:
        # Reuse a credential that already exists -- e.g. one a capa's own
        # setup form chose -- instead of asking the operator to retype
        # values already sitting in the credential store.
        try:
            cred = await get_credential(
                db,
                tenant_id=principal.tenant_id,
                credential_id=body.credential_id,
                owner="organisation",
            )
        except CredentialNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such credential") from exc
        if cred.credential_type != body.credential_type:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"credential {cred.name!r} is a {cred.credential_type!r}, "
                f"not a {body.credential_type!r}",
            )
    else:
        # `Credential.name` is unique per tenant (uq_credential_tenant_name):
        # a login created for a department is suffixed with the department's
        # name (not id) so the Credentials list stays readable.
        cred_name = f"{body.name} ({dept.name})" if dept is not None else body.name
        try:
            cred = await create_credential(
                db,
                tenant_id=principal.tenant_id,
                name=cred_name,
                credential_type=body.credential_type,
                field_values=body.field_values,
            )
        except SecretStoreUnavailable as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"a credential named {cred_name!r} already exists"
            ) from exc
        await db.flush()
    if agent is not None:
        assert actor is not None and integ is not None
        await save_tool_access(
            request,
            db,
            actor,
            agent,
            integ,
            SetAgentToolAccessRequest(mode="fixed", credential_id=cred.id),
        )
    elif dept is not None:
        # A department's login (the department's default for the tool): it
        # replaces any other login of the type there, and stays enabled for
        # any other department it serves.
        await choose_for_department(db, cred, dept.id)
    tool = await tool_connection(
        db,
        name=body.name,
        department_id=agent.department_id if agent is not None else body.department_id,
        connected_only=False,
    )
    dto = McpLoginDTO(
        id=str(cred.id),
        name=body.name,
        credential_id=str(cred.id),
        department_id=str(body.department_id) if body.department_id else None,
        connected=bool(tool is not None and tool.connected),
        scopes=tool.scopes if tool is not None else body.scopes,
        health=(tool.health or {}) if tool is not None else {},
    )
    await db.commit()
    return dto


async def _human_actor(db: AsyncSession, principal: Principal) -> HumanActor:
    """The caller as a person with a seat scope, resolved the way
    `require_agent_write` does."""
    try:
        member, scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requires a person")
    return HumanActor(principal=principal, member=member, scope=scope)


@router.get(
    "/mcp/logins",
    response_model=list[McpLoginDTO],
    dependencies=[Depends(require_permission(perm(INTEGRATION, VIEW)))],
)
async def list_logins(
    db: DbSession,
    principal: CurrentPrincipal,
    credential_type: str | None = Query(default=None, alias="credentialType"),
) -> list[McpLoginDTO]:
    stmt = select(m.McpConnection).where(
        m.McpConnection.tenant_id == principal.tenant_id,
        m.McpConnection.credential_id.is_not(None),
    )
    rows = (await db.execute(stmt.order_by(m.McpConnection.name))).scalars().all()
    if credential_type is not None:
        cred_ids = set(
            (
                await db.execute(
                    select(m.Credential.id).where(
                        m.Credential.tenant_id == principal.tenant_id,
                        m.Credential.credential_type == credential_type,
                    )
                )
            )
            .scalars()
            .all()
        )
        rows = [r for r in rows if r.credential_id in cred_ids]
    return [_to_dto(r) for r in rows]
