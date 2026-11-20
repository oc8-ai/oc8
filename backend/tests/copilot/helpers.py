"""A tenant with the Copilot and members who hold copilot:use through an
assigned role, each with their own Copilot chat session and task."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agent.engine import open_run_task
from oc8.authz.permissions import COPILOT_USE
from oc8.copilot.schedule import FollowupSpec


def once_in_an_hour() -> FollowupSpec:
    return FollowupSpec(
        "once", "Europe/Berlin", dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=1), None, None
    )


@dataclass(frozen=True)
class Seat:
    member_id: uuid.UUID
    subject: str
    session_id: uuid.UUID
    task_id: uuid.UUID


async def copilot_seat(
    db: AsyncSession, tenant: uuid.UUID, subject: str, *, with_role: bool = True
) -> Seat:
    assistant = await get_or_create_assistant(db, tenant_id=tenant)
    role_id = None
    if with_role:
        role = m.Role(tenant_id=tenant, name=f"copilot-{uuid.uuid4().hex[:8]}", kind="human")
        db.add(role)
        await db.flush()
        db.add(m.RolePermission(tenant_id=tenant, role_id=role.id, permission=COPILOT_USE))
        await db.flush()
        role_id = role.id
    member = m.OrgMember(
        tenant_id=tenant, subject=subject, subject_uuid=uuid.uuid4(), role_id=role_id
    )
    db.add(member)
    await db.flush()
    session = m.ChatSession(tenant_id=tenant, agent_id=assistant.id, member_id=member.id)
    db.add(session)
    await db.flush()
    task = await open_run_task(db, agent=assistant, task_text="chat", tenant_id=tenant)
    session.task_id = task.id
    await db.flush()
    return Seat(member.id, subject, session.id, task.id)
