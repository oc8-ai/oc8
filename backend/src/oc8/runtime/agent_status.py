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
  on the agent. Only executors wait for it (the reconciler only tries it), so
  it serialises exactly what the row lock serialised -- two deliveries of one
  run, and the one-run-per-agent check with its transition -- and no other
  writer of the agent ever waits on it.
* **"running"** (`mark_agent_running`): written in its own short transaction,
  so the row is free again at once. It is a display status (decisions read the
  run's state, never this). A run that fails in this process before its commit
  puts the status back (`RunningGuard`); one whose process dies is healed by
  the reconciler (`oc8.runtime.reconcile.release_stale_running_agents`).
* **Every later write** (`settle_agent_status`): compare-and-set against the
  status this run last wrote or read -- tracked on the session, never taken
  from the identity map, which any `populate_existing` re-read replaces -- and
  never over a pause (`pause_reason`). With the row no longer held, a pause or
  stop can land mid-run; it used to wait and then win, and it must still win
  rather than be overwritten by "idle".
"""

from __future__ import annotations

import logging
import uuid
from types import TracebackType

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.realtime.emit import publish_agent_status

logger = logging.getLogger(__name__)

RUNNING = "running"

#: How long marking an agent "running" may wait for its row. A writer holding
#: it must not hold up the run: the status then shows when the run next writes it.
MARK_LOCK_TIMEOUT = "2s"

#: `session.info` key: agent id -> the status this run may overwrite, or None
#: once somebody else has taken the status over (the run then never writes it).
_EXPECTED_KEY = "oc8_agent_status_expected"


def slot_key(agent_id: uuid.UUID) -> str:
    return f"agent_run_slot:{agent_id}"


async def take_run_slot(db: AsyncSession, agent_id: uuid.UUID) -> None:
    """Wait for, then hold until this transaction ends, the agent's run slot.

    Taken by every executor before it reads a run under lock. Not a row lock:
    the row stays writable for the whole run."""
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(slot_key(agent_id), 0)))
    )


def _expectations(db: AsyncSession) -> dict[uuid.UUID, str | None]:
    found: dict[uuid.UUID, str | None] = db.info.setdefault(_EXPECTED_KEY, {})
    return found


def remember_claimed_status(db: AsyncSession, agent: m.Agent) -> None:
    """Record the status read at the claim as the one this run may overwrite."""
    _expectations(db)[agent.id] = agent.status


def expected_status(db: AsyncSession, agent: m.Agent) -> str | None:
    """What this run may overwrite: the tracked value, or -- for a session that
    never claimed (the engine called directly) -- the status as loaded."""
    tracked = _expectations(db)
    if agent.id in tracked:
        return tracked[agent.id]
    loaded = agent.__dict__.get("status")
    return loaded if isinstance(loaded, str) else None


async def mark_agent_running(tenant_id: uuid.UUID, db: AsyncSession, agent: m.Agent) -> bool:
    """Show the agent as running, committed at once in a short transaction of
    its own -- only if its status is still the one the claim read and it is not
    paused. Returns whether it was marked.

    Not marked because somebody changed the status since the claim (a pause, a
    stop): the run leaves it to them, as the executor's early exits leave a
    paused agent alone, and never writes it later. Not marked because the row
    stayed locked past `MARK_LOCK_TIMEOUT`: logged, and the run goes on; its
    later writes still compare against the status the claim read."""
    expected = expected_status(db, agent)
    if expected is None:
        return False
    try:
        async with tenant_session(tenant_id) as side:
            await side.execute(text(f"SET LOCAL lock_timeout = '{MARK_LOCK_TIMEOUT}'"))
            marked = (
                await side.execute(
                    update(m.Agent)
                    .where(
                        m.Agent.id == agent.id,
                        m.Agent.status == expected,
                        m.Agent.pause_reason.is_(None),
                    )
                    .values(status=RUNNING)
                    .returning(m.Agent.id)
                    .execution_options(synchronize_session=False)
                )
            ).first() is not None
    except DBAPIError:
        logger.warning(
            "agent %s: its row stayed locked; not shown as running for now",
            agent.id,
            exc_info=True,
        )
        return False
    if not marked:
        logger.info("agent %s: status changed since the claim; the run leaves it alone", agent.id)
        _expectations(db)[agent.id] = None
        return False
    _expectations(db)[agent.id] = RUNNING
    set_committed_value(agent, "status", RUNNING)
    await publish_agent_status(agent)
    return True


async def settle_agent_status(db: AsyncSession, agent: m.Agent, status: str) -> bool:
    """Set the agent's status as a run ends or suspends -- only if it is still
    what this run last wrote or read (`expected_status`) and the agent is not
    paused. A pause, a stop or any other status somebody set meanwhile is left
    as it is. Returns whether the status was written; either way `agent` then
    holds what the database has."""
    expected = expected_status(db, agent)
    await db.flush()
    written = False
    if expected is not None:
        written = (
            await db.execute(
                update(m.Agent)
                .where(
                    m.Agent.id == agent.id,
                    m.Agent.status == expected,
                    m.Agent.pause_reason.is_(None),
                )
                .values(status=status)
                .returning(m.Agent.id)
                .execution_options(synchronize_session=False)
            )
        ).first() is not None
    _expectations(db)[agent.id] = status if written else None
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


class RunningGuard:
    """Puts "running" back when the run that set it never commits.

    `mark_agent_running` commits on its own; the run's transaction may still
    roll back (an exception before or after the runtime, a failed commit), and
    the run is then `queued` again with its agent shown "running". Entered
    OUTSIDE the run's `tenant_session`, so it acts after that rollback."""

    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.agent_id: uuid.UUID | None = None
        self.restore_to: str | None = None

    def arm(self, agent_id: uuid.UUID, restore_to: str) -> None:
        self.agent_id = agent_id
        self.restore_to = restore_to

    async def __aenter__(self) -> RunningGuard:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None or self.agent_id is None or self.restore_to is None:
            return
        await restore_after_failure(self.tenant_id, self.agent_id, self.restore_to)


async def restore_after_failure(tenant_id: uuid.UUID, agent_id: uuid.UUID, restore_to: str) -> None:
    """Best-effort: back to `restore_to` if the agent still shows "running"
    and is not paused. Never raises -- the run's own error is what matters."""
    try:
        async with tenant_session(tenant_id) as side:
            await side.execute(text(f"SET LOCAL lock_timeout = '{MARK_LOCK_TIMEOUT}'"))
            await side.execute(
                update(m.Agent)
                .where(
                    m.Agent.id == agent_id,
                    m.Agent.status == RUNNING,
                    m.Agent.pause_reason.is_(None),
                )
                .values(status=restore_to)
                .execution_options(synchronize_session=False)
            )
    except Exception:
        logger.warning(
            "agent %s: could not put its status back after a failed run; the reconciler will",
            agent_id,
            exc_info=True,
        )
