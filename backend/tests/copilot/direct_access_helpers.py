"""One member who may use one connected system through a department seat,
with their own Copilot conversation -- the direct-access fixture."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agent.engine import open_run_task

READ_WRITE: dict[str, Any] = {"enabled": True, "read": True, "modify": True}
READ_ONLY: dict[str, Any] = {"enabled": True, "read": True, "modify": False}
SCOPES: dict[str, list[str]] = {"read": ["search_records"], "modify": ["update_record"]}


@dataclass(frozen=True)
class DirectOffice:
    tenant: uuid.UUID
    copilot_id: uuid.UUID
    department_id: uuid.UUID
    member_id: uuid.UUID
    subject: str
    session_id: uuid.UUID
    task_id: uuid.UUID
    connection_id: uuid.UUID


async def direct_office(
    db: AsyncSession,
    tenant: uuid.UUID,
    *,
    policy: dict[str, Any] | None = None,
    seated: bool = True,
    connected: bool = True,
    subject: str = "anna",
) -> DirectOffice:
    copilot = await get_or_create_assistant(db, tenant_id=tenant)
    dept = m.Department(
        tenant_id=tenant, name="Sales", frame={"tools": {"things": dict(policy or READ_WRITE)}}
    )
    db.add(dept)
    await db.flush()
    member = m.OrgMember(
        tenant_id=tenant,
        subject=subject,
        subject_uuid=uuid.uuid4(),
        display_name=subject.title(),
    )
    db.add(member)
    await db.flush()
    if seated:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=member.id,
                department_id=dept.id,
                seat_role="dept_viewer",
            )
        )
        await db.flush()
    conn = m.McpConnection(
        tenant_id=tenant,
        department_id=dept.id,
        name="things",
        transport="stdio",
        server_url="stdio://things",
        connected=connected,
        config={"command": "x", "args": []},
        scopes=SCOPES,
    )
    db.add(conn)
    await db.flush()
    session = m.ChatSession(tenant_id=tenant, agent_id=copilot.id, member_id=member.id)
    db.add(session)
    await db.flush()
    task = await open_run_task(db, agent=copilot, task_text="chat", tenant_id=tenant)
    session.task_id = task.id
    await db.flush()
    return DirectOffice(
        tenant=tenant,
        copilot_id=copilot.id,
        department_id=dept.id,
        member_id=member.id,
        subject=subject,
        session_id=session.id,
        task_id=task.id,
        connection_id=conn.id,
    )


async def approved_offer(
    db: AsyncSession, office: DirectOffice, *, tool_key: str = "things", status: str = "approved"
) -> m.ApprovalRequest:
    row = m.ApprovalRequest(
        tenant_id=office.tenant,
        agent_id=office.copilot_id,
        task_id=office.task_id,
        action_type="direct_access",
        title="Copilot on behalf of Anna: read things",
        status=status,
        payload={
            "direct_access": {
                "member_id": str(office.member_id),
                "member_name": "Anna",
                "session_id": str(office.session_id),
                "tool_key": tool_key,
                "label": tool_key,
                "account": "organisation",
                "decider": "member",
            }
        },
    )
    db.add(row)
    await db.flush()
    return row


async def grant(
    db: AsyncSession,
    office: DirectOffice,
    *,
    tool_key: str = "things",
    session_id: uuid.UUID | None = None,
) -> m.DirectAccessGrant:
    approval = await approved_offer(db, office, tool_key=tool_key)
    row = m.DirectAccessGrant(
        tenant_id=office.tenant,
        chat_session_id=session_id or office.session_id,
        member_id=office.member_id,
        tool_key=tool_key,
        approval_id=approval.id,
    )
    db.add(row)
    await db.flush()
    return row


async def chat_run(
    db: AsyncSession,
    office: DirectOffice,
    *,
    door: str = "web",
    on_behalf: bool = True,
    source: str = "chat",
    state: str = "running",
    extra: dict[str, Any] | None = None,
) -> m.AgentRun:
    ctx: dict[str, Any] = {
        "task": "find the quotation",
        "chat_session_id": str(office.session_id),
        "door": door,
        "originating_operator": office.subject,
    }
    if on_behalf:
        ctx["on_behalf_of"] = str(office.member_id)
    ctx.update(extra or {})
    run = m.AgentRun(
        tenant_id=office.tenant,
        agent_id=office.copilot_id,
        task_id=office.task_id,
        source=source,
        state=state,
        context=ctx,
    )
    db.add(run)
    await db.flush()
    return run
