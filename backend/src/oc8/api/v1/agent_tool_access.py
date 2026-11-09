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
from oc8.connections.access import binding_fingerprint, department_targets, unconfirmed_reason
from oc8.connections.integrations import Integration, installed_integrations
from oc8.schemas.dto import AccessOptionDTO, AgentToolAccessDTO
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


async def _dto(
    db: AsyncSession, agent: m.Agent, integ: Integration, row: m.AgentToolAccess | None
) -> AgentToolAccessDTO:
    base = AgentToolAccessDTO(
        tool_key=integ.tool_key,
        integration=integ.name,
        label=integ.label,
        kind=integ.kind,
        mode="default" if row is None else row.mode,
        default_mode="user" if integ.kind == "oauth" else "organisation",
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
    unprocessable = status.HTTP_422_UNPROCESSABLE_CONTENT
    oauth_id: uuid.UUID | None = None
    credential_id: uuid.UUID | None = None
    owner: uuid.UUID | None = None
    target_label: str | None = None
    admin = await may_manage_integrations(request, db, actor.principal)
    consent: str | None = None
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
        "set_by_member_id": str(row.set_by_member_id) if row and row.set_by_member_id else None,
        "set_with_manage": row.set_with_manage if row else None,
    }
    if row is None:
        row = m.AgentToolAccess(
            tenant_id=agent.tenant_id, agent_id=agent.id, tool_key=tool_key, mode=body.mode
        )
        db.add(row)
    row.mode = body.mode
    row.oauth_connection_id = oauth_id
    row.credential_id = credential_id
    row.set_by_member_id = actor.member.id
    # An administrator's choice stands on its own (an organisation account or a
    # colleague's); otherwise it rests on the owner or the department's setup.
    row.set_with_manage = consent in ("manage", "organisation") and admin
    row.binding_fingerprint = await binding_fingerprint(
        db, agent, tool_key, version_id=agent.current_version_id
    )
    await db.flush()
    await append_event(
        db,
        tenant_id=agent.tenant_id,
        actor_type="operator",
        actor_id=actor.member.id,
        category="authz",
        action="agent.tool_access.updated",
        resource={
            "agent_id": str(agent.id),
            "tool": tool_key,
            "mode": body.mode,
            "oauth_connection_id": str(oauth_id) if oauth_id else None,
            "credential_id": str(credential_id) if credential_id else None,
            "target_label": target_label,
            "target_owner_member_id": str(owner) if owner else None,
            "previous": previous,
            "consent": consent,
            "confirm_share": bool(body.confirm_share),
        },
        principal=actor.principal,
    )
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
