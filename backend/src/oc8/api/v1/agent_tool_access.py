"""The agent's "Zugang" setting per connectable tool (connections design).

Writing needs the right to change this agent. Binding an agent to a PERSON's
account additionally needs that person, or `integration:manage`: otherwise
anybody who may edit an agent could make it act as a colleague.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import DbSession, authorize_agent_write, require_agent_write
from oc8.api.v1.connections import may_manage_integrations
from oc8.audit import append_event
from oc8.authz.scope import HumanActor
from oc8.connections.access import (
    ConnectionRequired,
    agent_login,
    department_setup,
    department_targets,
    unconfirmed_reason,
)
from oc8.connections.access_write import write_tool_access
from oc8.connections.credential_env import copied_missing
from oc8.connections.integrations import Integration, installed_integrations
from oc8.connections.login import holds_copied_login
from oc8.connections.tool_connection import connection_for_context, tool_connection
from oc8.credentials.registry import CredentialTypeNotFound, get_credential_type
from oc8.credentials.service import missing_required, require_complete
from oc8.schemas.dto import AccessOptionDTO, AgentToolAccessDTO, EffectiveLoginDTO
from oc8.schemas.requests import SetAgentToolAccessRequest

router = APIRouter()


async def _agent(
    request: Request, db: AsyncSession, actor: HumanActor, agent_id: uuid.UUID
) -> m.Agent:
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    return agent


async def _integrations(db: AsyncSession) -> dict[str, Integration]:
    return {
        i.integration.tool_key: i.integration
        for i in await installed_integrations(db)
        if i.available
    }


async def _names(db: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (
        await db.execute(
            select(m.OrgMember.id, m.OrgMember.display_name).where(m.OrgMember.id.in_(wanted))
        )
    ).all()
    return {r.id: r.display_name for r in rows}


async def _default_mode(db: AsyncSession, agent: m.Agent, integ: Integration) -> str:
    """What the runtime uses without an access row (access._default): the
    department's own setup -- for an OAuth integration an app-only access
    reaching the organisation's accounts -- else the user's context."""
    if integ.kind != "oauth":
        return "organisation"
    # The tool's row the agent runs on: a department's app-only setup lives
    # on the row it still owns (unconsolidated); the shared row has none.
    conn = await tool_connection(
        db, name=integ.tool_key, department_id=agent.department_id, connected_only=False
    )
    return "organisation" if conn is not None and department_setup(conn, agent, integ) else "user"


async def _dto(
    db: AsyncSession, agent: m.Agent, integ: Integration, row: m.AgentToolAccess | None
) -> AgentToolAccessDTO:
    base = AgentToolAccessDTO(
        tool_key=integ.tool_key,
        integration=integ.name,
        label=integ.label,
        kind=integ.kind,
        mode="default" if row is None else row.mode,
        default_mode=await _default_mode(db, agent, integ),
    )
    if row is None or row.mode != "fixed":
        return base
    target: tuple[uuid.UUID, uuid.UUID | None, str] | None = None
    update: dict[str, object] = {}
    if row.oauth_connection_id is not None:
        conn = await db.get(m.OAuthConnection, row.oauth_connection_id)
        if conn is not None:
            target = (conn.id, conn.owner_member_id, conn.account_label)
            update["oauth_connection_id"] = str(conn.id)
    if target is None and row.credential_id is not None:
        cred = await db.get(m.Credential, row.credential_id)
        if cred is not None:
            target = (cred.id, cred.owner_member_id, cred.name)
            update["credential_id"] = str(cred.id)
    if target is None:
        return base.model_copy(update={"target_missing": True})
    target_id, owner, label = target
    names = await _names(db, {owner})
    # The same rule the runtime applies, against the agent as it is now.
    reason = await unconfirmed_reason(
        db, row, agent, integ.tool_key, target_id, owner, version_id=agent.current_version_id
    )
    return base.model_copy(
        update={
            **update,
            "target_label": label,
            "target_owner_name": names.get(owner) if owner else None,
            "needs_confirmation": reason is not None,
            "confirmation_reason": reason,
        }
    )


@router.get("/agents/{agent_id}/tool-access", response_model=list[AgentToolAccessDTO])
async def list_tool_access(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> list[AgentToolAccessDTO]:
    agent = await _agent(request, db, actor, agent_id)
    rows = {
        r.tool_key: r
        for r in (
            await db.execute(
                select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent.id)
            )
        ).scalars()
    }
    return [
        await _dto(db, agent, integ, rows.get(key))
        for key, integ in sorted((await _integrations(db)).items())
    ]


@router.get(
    "/agents/{agent_id}/tool-access/{tool_key}/options", response_model=list[AccessOptionDTO]
)
async def list_options(
    agent_id: uuid.UUID,
    tool_key: str,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> list[AccessOptionDTO]:
    agent = await _agent(request, db, actor, agent_id)
    integ = (await _integrations(db)).get(tool_key)
    if integ is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no connectable integration for this tool")
    admin = await may_manage_integrations(request, db, actor.principal)
    me = actor.member.id
    out = [AccessOptionDTO(kind="user", label="user")]
    if integ.kind == "oauth":
        rows = (
            (
                await db.execute(
                    select(m.OAuthConnection)
                    .where(
                        m.OAuthConnection.provider == integ.provider,
                        m.OAuthConnection.grant_type != "device_code",
                    )
                    .order_by(m.OAuthConnection.created_at)
                )
            )
            .scalars()
            .all()
        )
        items = [(r.id, r.account_label, r.owner_member_id) for r in rows]
    else:
        creds = (
            (
                await db.execute(
                    select(m.Credential)
                    .where(m.Credential.credential_type == integ.credential_type)
                    .order_by(m.Credential.created_at)
                )
            )
            .scalars()
            .all()
        )
        items = [(c.id, c.name, c.owner_member_id) for c in creds]
    dept_ids = set(await department_targets(db, agent, tool_key))
    names = await _names(db, {owner for _, _, owner in items})
    kind = "oauth" if integ.kind == "oauth" else "credential"
    for owner_group in ("organisation", "me", "member"):
        for row_id, label, owner in items:
            group = "organisation" if owner is None else ("me" if owner == me else "member")
            if group != owner_group or (group == "member" and not admin):
                continue
            if group == "organisation" and not admin and row_id not in dept_ids:
                continue
            out.append(
                AccessOptionDTO(
                    kind=kind,
                    id=str(row_id),
                    label=label,
                    owner=group,
                    owner_name=names.get(owner) if owner is not None else None,
                )
            )
    return out


async def _run_connection(
    db: AsyncSession, agent: m.Agent, tool_key: str
) -> m.McpConnection | None:
    """The row an agent's run reaches `tool_key` through, as the executor
    picks it: the agent's own narrowing pin, else its department's default
    connection, else the tool's row. A pin that names nothing or another
    tool: None (the run would fail)."""
    tools = (agent.narrowing or {}).get("tools")
    policy = tools.get(tool_key) if isinstance(tools, dict) else None
    pinned = policy.get("connection_id") if isinstance(policy, dict) else None
    if not pinned and agent.department_id is not None:
        dept = await db.get(m.Department, agent.department_id)
        frame_tools = (dept.frame or {}).get("tools") if dept is not None else None
        entry = frame_tools.get(tool_key) if isinstance(frame_tools, dict) else None
        pinned = entry.get("default_connection_id") if isinstance(entry, dict) else None
    if pinned:
        conn = await connection_for_context(db, pinned)
        return conn if conn is not None and conn.name == tool_key else None
    return await tool_connection(
        db, name=tool_key, department_id=agent.department_id, connected_only=False
    )


def _live_chat(agent: m.Agent, viewer: uuid.UUID) -> m.AgentRun:
    """A run like the viewer's own live web chat with the agent -- never
    stored -- so the login is chosen exactly as such a run would."""
    return m.AgentRun(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        agent_version_id=agent.current_version_id,
        source="chat",
        context={
            "on_behalf_of": str(viewer),
            "chat_session_id": str(uuid.uuid4()),
            "door": "web",
        },
    )


@router.get("/agents/{agent_id}/tool-access/{tool_key}/effective", response_model=EffectiveLoginDTO)
async def effective_login(
    agent_id: uuid.UUID,
    tool_key: str,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> EffectiveLoginDTO:
    """The login the tool uses in the caller's own chat with this agent,
    where it comes from, and -- when none can be used -- why."""
    agent = await _agent(request, db, actor, agent_id)
    integ = (await _integrations(db)).get(tool_key)
    if integ is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no connectable integration for this tool")
    conn = await _run_connection(db, agent, tool_key)
    if conn is None:
        return EffectiveLoginDTO(reason="not_configured")
    try:
        # The runtime's own path (`resolve_tool_access`), for a live chat of
        # the person looking: the agent's choice with its consent checks, an
        # older pinned login row, their own login, the department's.
        _acting, _integration, choice = await agent_login(
            db, run=_live_chat(agent, actor.member.id), agent=agent, conn=conn
        )
    except ConnectionRequired as exc:
        return EffectiveLoginDTO(reason=exc.reason, missing=exc.missing)
    if choice is None:
        return EffectiveLoginDTO(source="legacy")
    if choice.oauth_connection is not None:
        return EffectiveLoginDTO(
            credential_id=str(choice.oauth_connection.id),
            credential_name=choice.oauth_connection.account_label,
            source=choice.source,
        )
    cred = choice.credential
    if cred is None and choice.legacy_conn is not None and choice.legacy_conn.credential_id:
        # A row pinned to one login, not yet consolidated: that login.
        cred = await db.get(m.Credential, choice.legacy_conn.credential_id)
    if cred is None:
        legacy = choice.legacy_conn
        if legacy is not None and integ.credential_type and holds_copied_login(legacy, integ):
            # A department's own copied login, not yet consolidated: shown as
            # incomplete when it is, as the run would refuse it.
            try:
                copied_type = await get_credential_type(
                    db, tenant_id=agent.tenant_id, name=integ.credential_type
                )
            except CredentialTypeNotFound:
                return EffectiveLoginDTO(source=choice.source)
            cfg = legacy.config if isinstance(legacy.config, dict) else {}
            missing = copied_missing(cfg, copied_type, integ.credential_env_fields)
            if missing:
                return EffectiveLoginDTO(
                    source=choice.source, reason="incomplete", missing=missing
                )
        return EffectiveLoginDTO(source=choice.source)
    out = EffectiveLoginDTO(
        credential_id=str(cred.id), credential_name=cred.name, source=choice.source
    )
    try:
        cred_type = await get_credential_type(
            db, tenant_id=agent.tenant_id, name=cred.credential_type
        )
    except CredentialTypeNotFound:
        return out
    missing = missing_required(cred_type, cred.field_values or {}, cred.secret_refs or {})
    if missing:
        return out.model_copy(update={"reason": "incomplete", "missing": missing})
    return out


async def connectable_integration(db: AsyncSession, tool_key: str) -> Integration | None:
    """The available connectable integration of `tool_key`, if any."""
    return (await _integrations(db)).get(tool_key)


async def authorized_agent(
    request: Request, db: AsyncSession, actor: HumanActor, agent_id: uuid.UUID
) -> m.Agent:
    """The agent, when `actor` may change it (404/403 otherwise)."""
    return await _agent(request, db, actor, agent_id)


async def save_tool_access(
    request: Request,
    db: AsyncSession,
    actor: HumanActor,
    agent: m.Agent,
    integ: Integration,
    body: SetAgentToolAccessRequest,
) -> m.AgentToolAccess:
    """Validate and write the agent's access to `integ`'s tool: who may bind
    which account, the binding fingerprint, and the audit event. The one
    writer of `AgentToolAccess` for every route; the caller authorized the
    agent (`authorized_agent`) and commits. Flushes, never commits."""
    tool_key = integ.tool_key
    unprocessable = status.HTTP_422_UNPROCESSABLE_CONTENT
    oauth_id: uuid.UUID | None = None
    credential_id: uuid.UUID | None = None
    owner: uuid.UUID | None = None
    target_label: str | None = None
    admin = await may_manage_integrations(request, db, actor.principal)
    consent: str | None = None
    chosen_login: m.Credential | None = None
    if body.mode == "user":
        if body.oauth_connection_id is not None or body.credential_id is not None:
            raise HTTPException(unprocessable, "user mode names no target")
    else:
        if (
            integ.kind == "oauth"
            and body.oauth_connection_id is not None
            and body.credential_id is None
        ):
            target = await db.get(m.OAuthConnection, body.oauth_connection_id)
            if (
                target is None
                or target.provider != integ.provider
                or target.grant_type == "device_code"
            ):
                raise HTTPException(unprocessable, "not a connection of this integration")
            oauth_id, owner, target_label = target.id, target.owner_member_id, target.account_label
        elif (
            integ.kind == "credential"
            and body.credential_id is not None
            and body.oauth_connection_id is None
        ):
            cred = await db.get(m.Credential, body.credential_id)
            if cred is None or cred.credential_type != integ.credential_type:
                raise HTTPException(unprocessable, "not a login of this integration")
            credential_id, owner, target_label = cred.id, cred.owner_member_id, cred.name
            chosen_login = cred
        else:
            raise HTTPException(unprocessable, "a fixed access names exactly one matching target")
        target_id = oauth_id or credential_id
        if owner is None:
            if not admin and target_id not in await department_targets(db, agent, tool_key):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "only an administrator may bind an agent to an organisation account",
                )
            consent = "organisation"
        elif owner == actor.member.id:
            if not body.confirm_share:
                raise HTTPException(
                    unprocessable, "binding your own account shares it: confirmShare is required"
                )
            consent = "owner"
        elif admin:
            consent = "manage"
        else:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "only its owner or an administrator may bind an agent to a person's account",
            )
    if chosen_login is not None:
        # An incomplete login is refused as a choice, naming its empty fields
        # (422 `credential_incomplete`), as the tool setup does -- only after
        # every check above, so a caller who may not bind the login learns
        # nothing about it.
        await require_complete(db, chosen_login)
    # An administrator's choice stands on its own (an organisation account or a
    # colleague's); otherwise it rests on the owner or the department's setup.
    return await write_tool_access(
        db,
        agent=agent,
        tool_key=tool_key,
        mode=body.mode,
        oauth_connection_id=oauth_id,
        credential_id=credential_id,
        set_by_member_id=actor.member.id,
        set_with_manage=consent in ("manage", "organisation") and admin,
        actor_type="operator",
        actor_id=actor.member.id,
        principal=actor.principal,
        audit={
            "target_label": target_label,
            "target_owner_member_id": str(owner) if owner else None,
            "consent": consent,
            "confirm_share": bool(body.confirm_share),
        },
    )


@router.put("/agents/{agent_id}/tool-access/{tool_key}", response_model=AgentToolAccessDTO)
async def set_tool_access(
    agent_id: uuid.UUID,
    tool_key: str,
    body: SetAgentToolAccessRequest,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentToolAccessDTO:
    agent = await _agent(request, db, actor, agent_id)
    integ = (await _integrations(db)).get(tool_key)
    if integ is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no connectable integration for this tool")
    row = await save_tool_access(request, db, actor, agent, integ, body)
    dto = await _dto(db, agent, integ, row)
    await db.commit()
    return dto


@router.delete("/agents/{agent_id}/tool-access/{tool_key}", status_code=status.HTTP_204_NO_CONTENT)
async def reset_tool_access(
    agent_id: uuid.UUID,
    tool_key: str,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> None:
    agent = await _agent(request, db, actor, agent_id)
    row = await db.scalar(
        select(m.AgentToolAccess).where(
            m.AgentToolAccess.agent_id == agent.id, m.AgentToolAccess.tool_key == tool_key
        )
    )
    if row is None and tool_key not in await _integrations(db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no connectable integration for this tool")
    if row is not None:
        previous = {
            "mode": row.mode,
            "oauth_connection_id": str(row.oauth_connection_id)
            if row.oauth_connection_id
            else None,
            "credential_id": str(row.credential_id) if row.credential_id else None,
            "set_by_member_id": str(row.set_by_member_id) if row.set_by_member_id else None,
            "set_with_manage": row.set_with_manage,
        }
        await db.delete(row)
        await append_event(
            db,
            tenant_id=agent.tenant_id,
            actor_type="operator",
            actor_id=actor.member.id,
            category="authz",
            action="agent.tool_access.reset",
            resource={"agent_id": str(agent.id), "tool": tool_key, "previous": previous},
            principal=actor.principal,
        )
    await db.commit()
