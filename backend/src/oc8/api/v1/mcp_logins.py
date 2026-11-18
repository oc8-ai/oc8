"""Create/list tool logins (unified connections design, "Storage").

Creating a login makes (or reuses) an organisation `Credential`; for an agent
it becomes the agent's fixed access (`AgentToolAccess`). No connection row is
created per login any more -- the tool has one row, and the run resolves which
login it starts with. The listing still shows the login-pinned rows older
versions created, until the tenant's rows are consolidated."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.audit import append_event
from oc8.auth import Principal
from oc8.authz.permissions import INTEGRATION, MANAGE, VIEW, perm
from oc8.connections.access import binding_fingerprint
from oc8.connections.integrations import installed_integrations
from oc8.connections.tool_connection import tool_connection
from oc8.credentials.enablement import department_ids_by_credential, set_departments
from oc8.credentials.registry import CredentialTypeNotFound, get_credential_type
from oc8.credentials.service import CredentialNotFound, create_credential, get_credential
from oc8.schemas.dto import McpLoginDTO
from oc8.schemas.requests import CreateMcpLoginRequest
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
    body: CreateMcpLoginRequest, db: DbSession, principal: CurrentPrincipal
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
    if body.agent_id is not None:
        agent = await db.get(m.Agent, body.agent_id)
        if agent is None or agent.deleted_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
        if not any(
            i.available
            and i.integration.tool_key == body.name
            and i.integration.kind == "credential"
            and i.integration.credential_type == body.credential_type
            for i in await installed_integrations(db)
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
        await _fix_agent_login(db, principal, agent, body.name, cred)
    elif dept is not None:
        # A department's login (the department's default for the tool): the
        # login is enabled for it, alongside any other department it serves.
        current = set((await department_ids_by_credential(db, [cred.id])).get(cred.id, []))
        if dept.id not in current:
            await set_departments(
                db,
                cred,
                department_ids=current | {dept.id},
                all_departments=cred.all_departments,
            )
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


async def _fix_agent_login(
    db: AsyncSession,
    principal: Principal,
    agent: m.Agent,
    tool_key: str,
    cred: m.Credential,
) -> None:
    """The agent's fixed access to `tool_key` through the organisation login
    `cred`, set by an administrator (integration:manage) -- written like
    `PUT /agents/{id}/tool-access/{key}` writes it, fingerprint included."""
    member_id = await db.scalar(
        select(m.OrgMember.id).where(m.OrgMember.subject == principal.subject)
    )
    row = await db.scalar(
        select(m.AgentToolAccess).where(
            m.AgentToolAccess.agent_id == agent.id, m.AgentToolAccess.tool_key == tool_key
        )
    )
    previous = {
        "mode": row.mode if row else None,
        "oauth_connection_id": str(row.oauth_connection_id)
        if row and row.oauth_connection_id
        else None,
        "credential_id": str(row.credential_id) if row and row.credential_id else None,
    }
    if row is None:
        row = m.AgentToolAccess(
            tenant_id=agent.tenant_id, agent_id=agent.id, tool_key=tool_key, mode="fixed"
        )
        db.add(row)
    row.mode = "fixed"
    row.oauth_connection_id = None
    row.credential_id = cred.id
    row.set_by_member_id = member_id
    row.set_with_manage = True
    row.binding_fingerprint = await binding_fingerprint(
        db, agent, tool_key, version_id=agent.current_version_id
    )
    await db.flush()
    await append_event(
        db,
        tenant_id=agent.tenant_id,
        actor_type="operator",
        actor_id=member_id,
        category="authz",
        action="agent.tool_access.updated",
        resource={
            "agent_id": str(agent.id),
            "tool": tool_key,
            "mode": "fixed",
            "oauth_connection_id": None,
            "credential_id": str(cred.id),
            "target_label": cred.name,
            "target_owner_member_id": None,
            "previous": previous,
            "consent": "organisation",
            "confirm_share": False,
        },
        principal=principal,
    )


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
