"""An agent's status and run slot, without holding its row for a whole run.

The in-process runtime runs an entire turn in ONE transaction, from the
executor's claim to its terminal commit. Anything that transaction locks stays
locked while the model thinks -- with a slow local model, for minutes. The
agent row used to be one of those locks, twice over: the claim took it
`FOR UPDATE` (so two executors never start the same run, or two runs of one
agent) and the transition wrote `status = 'running'` into it. Every write to
the agent -- a model switch, a lifecycle pause, the Copilot's own model and
mission sync -- then waited for the turn to end.

So the three things that lock gave are split up:

* **The run slot** (`take_run_slot`): a transaction-scoped advisory lock keyed
  on the agent. Only executors take it, so it serialises exactly what the row
  lock serialised -- two deliveries of one run, and the one-run-per-agent check
  with its transition -- and nobody else ever waits on it.
* **"running"** (`mark_agent_running`): written in its own short transaction,
  so the row is free again at once. It is a display status (decisions read the
  run's state, never this); a crash before the run's commit leaves it showing
  "running" until the redelivered run ends it, as an isolated run always did.
* **The ending** (`settle_agent_status`): the run's own write of the agent's
  next status, made only if nobody else changed it meanwhile. With the row no
  longer held, a pause or stop can land mid-run; it used to wait and then win,
  and it must still win rather than be overwritten by "idle".
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.realtime.emit import publish_agent_status

RUNNING = "running"


async def take_run_slot(db: AsyncSession, agent_id: uuid.UUID) -> None:
    """Wait for, then hold until this transaction ends, the agent's run slot.

    Taken by every executor before it reads a run under lock, and by nothing
    else. Not a row lock: the row stays writable for the whole run."""
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"agent_run_slot:{agent_id}", 0)))
    )


async def mark_agent_running(tenant_id: uuid.UUID, agent: m.Agent) -> None:
    """Show the agent as running, committed at once in a short transaction of
    its own, and make the run's copy agree without making it dirty -- so the
    run's transaction never writes, and so never locks, the agent row for it."""
    async with tenant_session(tenant_id) as side:
        await side.execute(
            update(m.Agent)
            .where(m.Agent.id == agent.id)
            .values(status=RUNNING)
            .execution_options(synchronize_session=False)
        )
    set_committed_value(agent, "status", RUNNING)
    await publish_agent_status(agent)


async def settle_agent_status(db: AsyncSession, agent: m.Agent, status: str) -> bool:
    """Set the agent's status as a run ends or suspends -- unless somebody else
    changed it since this session last saw it (a pause, a stop, a delete during
    the run). Compare-and-set against that last-seen value, so the run's own
    earlier writes (a budget hard-stop pausing itself) behave exactly as an
    unconditional write did. Returns whether the status was written; either
    way `agent` then holds what the database has."""
    history = inspect(agent).attrs.status.history
    known = history.deleted[0] if history.deleted else agent.status
    if history.has_changes():
        set_committed_value(agent, "status", known)
    await db.flush()
    written = (
        await db.execute(
            update(m.Agent)
            .where(m.Agent.id == agent.id, m.Agent.status == known)
            .values(status=status)
            .returning(m.Agent.id)
            .execution_options(synchronize_session=False)
        )
    ).first() is not None
    current = (
        await db.execute(
            select(m.Agent.status, m.Agent.pause_reason, m.Agent.paused_at).where(
                m.Agent.id == agent.id
            )
        )
    ).first()
    if current is not None:
        set_committed_value(agent, "status", current.status)
        set_committed_value(agent, "pause_reason", current.pause_reason)
        set_committed_value(agent, "paused_at", current.paused_at)
    if written:
        await publish_agent_status(agent)
    return written
