"""The member-facing side of the Copilot's personal notes (§7a.5)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m


async def member_behind_run_task(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task: m.Task | None,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """The member whose chat session opened `task`, or None.

    None as well when `run_id` is a chat run someone OTHER than that member
    posted (the `copilot:manage` oversight carve-out) -- the same rule as
    `control_tools._resolve_agent_actor`: an operator replying in a
    colleague's session neither reads nor writes that colleague's notes."""
    if task is None:
        return None
    from oc8.agent.control_tools import _member_behind_task

    member = await _member_behind_task(db, tenant_id=tenant_id, task=task)
    if member is None:
        return None
    if run_id is not None:
        run = await db.get(m.AgentRun, run_id)
        if run is not None and run.tenant_id == tenant_id and run.source == "chat":
            operator = (run.context or {}).get("originating_operator")
            if isinstance(operator, str) and operator and operator != member.subject:
                return None
    return member.id


async def _copilot_store_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, assistant_id: uuid.UUID
) -> uuid.UUID | None:
    store_id: uuid.UUID | None = await db.scalar(
        select(m.MemoryStore.id).where(
            m.MemoryStore.tenant_id == tenant_id,
            m.MemoryStore.tier == "agent",
            m.MemoryStore.owner_id == assistant_id,
        )
    )
    return store_id


async def list_notes(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, assistant_id: uuid.UUID
) -> list[m.MemoryRecord]:
    store_id = await _copilot_store_id(db, tenant_id=tenant_id, assistant_id=assistant_id)
    if store_id is None:
        return []
    rows = await db.execute(
        select(m.MemoryRecord)
        .where(
            m.MemoryRecord.store_id == store_id,
            m.MemoryRecord.record_metadata["member_id"].astext == str(member_id),
        )
        .order_by(m.MemoryRecord.created_at.desc())
    )
    return list(rows.scalars())


async def delete_note(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    assistant_id: uuid.UUID,
    note_id: uuid.UUID,
) -> bool:
    for note in await list_notes(
        db, tenant_id=tenant_id, member_id=member_id, assistant_id=assistant_id
    ):
        if note.id == note_id:
            await db.delete(note)
            await db.flush()
            return True
    return False


async def delete_member_notes(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> None:
    from oc8.agent.assistant import get_or_create_assistant

    assistant = await get_or_create_assistant(db, tenant_id=tenant_id)
    store_id = await _copilot_store_id(db, tenant_id=tenant_id, assistant_id=assistant.id)
    if store_id is None:
        return
    await db.execute(
        delete(m.MemoryRecord).where(
            m.MemoryRecord.store_id == store_id,
            m.MemoryRecord.record_metadata["member_id"].astext == str(member_id),
        )
    )
