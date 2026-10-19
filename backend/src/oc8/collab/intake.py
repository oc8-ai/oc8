"""An accepted handoff becomes work on somebody's desk.

§14a.3 says an intake "materializes as a new task assigned to the team-lead
agent, who decomposes it per delegation". Until now `accept_handoff` only moved
a status and recorded a `target_task_id` **the caller had to supply**. Nothing
created the task, nothing found the lead, nothing started a run — so accepting a
handoff produced a row and no work. Measured before writing this: 413 tasks in
the database, none delegated, and no run has ever had `source="delegation"`.

That also explains what a team lead is FOR here, which is worth saying because
the obvious answer is wrong. Two agents already work one queue in parallel: the
record claim keeps them off each other, and adding a coordinator to hand out
tickets would cost two runs per ticket and buy nothing. A lead earns its place
where work arrives as ONE lump that somebody has to cut up — which is exactly
what a handoff from another department is.

Deliberately at the request boundary rather than inside `accept_handoff`:
enqueueing a run commits, and a domain function that quietly commits its
caller's transaction is a trap this codebase has been bitten by before.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.runtime.intake import enqueue_run
from oc8.tenants.activity import TenantInactive

logger = logging.getLogger(__name__)


def brief(handoff: m.Handoff, type_name: str) -> str:
    """The task text a lead is handed. Readable, and complete enough to act on.

    The payload is included verbatim: it is the whole of what the sending
    department chose to say, and summarising it here would be this system
    deciding what the other department meant.
    """
    lines = [
        f"Übergabe aus einer anderen Abteilung: {type_name}.",
        "",
        "Das ist EIN Auftrag, kein Ticket. Zerlege ihn in Teilaufgaben und gib "
        "sie an deine Kolleginnen und Kollegen weiter (delegate_task), soweit "
        "du sie nicht selbst erledigen musst. Melde dich, wenn alles verteilt "
        "ist.",
        "",
        "Inhalt der Übergabe:",
    ]
    for key, value in (handoff.payload or {}).items():
        lines.append(f"- {key}: {value}")
    if not (handoff.payload or {}):
        lines.append("- (leer)")
    return "\n".join(lines)


async def route_to_team_lead(
    db: AsyncSession, handoff: m.Handoff, *, tenant_id: uuid.UUID
) -> uuid.UUID | None:
    """Put an accepted handoff on the target department's lead as a task.

    Returns the task id, or None when there is nobody to give it to -- a
    department without a lead accepts the handoff and leaves it for a human,
    which is honest: inventing an assignee would hide that nobody is on it.
    """
    dept = await db.get(m.Department, handoff.target_department_id)
    if dept is None or dept.team_lead_agent_id is None:
        logger.info(
            "handoff %s accepted, but department %s has no team lead to route it to",
            handoff.id,
            handoff.target_department_id,
        )
        return None
    lead = await db.get(m.Agent, dept.team_lead_agent_id)
    if lead is None or lead.deleted_at is not None:
        return None

    handoff_type = await db.get(m.HandoffType, handoff.handoff_type_id)
    type_name = handoff_type.name if handoff_type is not None else "Übergabe"

    task = m.Task(
        tenant_id=tenant_id,
        department_id=dept.id,
        assigned_agent_id=lead.id,
        title=f"Übergabe: {type_name}",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    handoff.target_task_id = task.id
    await db.flush()

    # `handoff:<id>` rather than the task: a redelivery of the same acceptance
    # must not start the work twice, and the handoff is what was accepted once.
    #
    # `task_id` matters more than it looks: without it the engine opens a task
    # of its OWN when the run starts, and the board shows the same handoff twice
    # -- once as "Übergabe: …" with nobody working it, once titled with the
    # whole briefing text. Observed live before it was passed. It has to travel
    # with the run at creation, not be patched on afterwards: a worker can pick
    # the run up in between and open the duplicate before the update lands.
    try:
        await enqueue_run(
            db,
            tenant_id=tenant_id,
            agent_id=lead.id,
            context={"task": brief(handoff, type_name)},
            source="handoff",
            idempotency_key=f"handoff:{handoff.id}",
            task_id=task.id,
        )
    except TenantInactive:
        # Refused before anything was committed: the task stays on the lead's
        # desk with the caller's transaction, but no run starts on it.
        logger.info(
            "handoff %s routed to task %s, but no run started: organisation %s is not active",
            handoff.id,
            task.id,
            tenant_id,
        )
        return task.id
    # `enqueue_run` COMMITS, and `set_config('app.tenant_id', …, is_local => true)`
    # ends with that commit. Anything the caller reads afterwards -- the DTO it
    # returns, the task it just created -- would run unbound and see nothing.
    # Re-binding here rather than in the handler keeps the trap with the code
    # that springs it, instead of with everyone who calls it later.
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
    )
    logger.info(
        "handoff %s routed to team lead %s as task %s", handoff.id, lead.name, task.id
    )
    return task.id
