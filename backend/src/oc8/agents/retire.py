"""Retiring a deleted agent: nothing it set up may keep working once it is gone.

Shared by every path that deletes an agent -- `DELETE /agents/{id}` and the
cascade of `DELETE /departments/{id}` -- so the two cannot drift. add/flush
only: the endpoints own the commit.

Copilot follow-ups (rows with a `chat_session_id`) are a member's own and are
managed in the Copilot, so they are not touched here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.runtime.cancellation import record_operator_cancel
from oc8.runtime.states import TERMINAL

if TYPE_CHECKING:
    from oc8.auth.principal import Principal

#: `trigger.last_skip_reason` of a trigger turned off because its agent was
#: archived, and the `cancellation_kind` of a run stopped for that reason.
AGENT_ARCHIVED = "agent_archived"
#: `trigger.last_skip_reason` of a trigger whose agent row no longer exists.
AGENT_MISSING = "agent_missing"
#: What a run stopped because its agent was archived reports as its output.
AGENT_ARCHIVED_OUTPUT = "Cancelled: the agent was deleted"


async def retire_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    hard_delete: bool,
    principal: Principal | None,
    resource_extra: dict[str, str] | None = None,
) -> None:
    """Stop everything that would make a deleted agent work again.

    - **Hard delete** (the agent never ran): its triggers are deleted with it.
      `trigger.agent_id` has no foreign key, so a left-over trigger would fire
      into a missing agent on every tick and nobody could see it to remove it.
    - **Archive**: its triggers are disabled, not deleted, so a restore keeps
      them for the operator to turn back on deliberately. Every unfinished
      run is cancelled through the same `run_cancellation` row an operator's
      cancel writes: a queued run is skipped at pickup, a running one stops
      at its next step boundary, a suspended one when it is next resumed.
    """
    own_triggers = (m.Trigger.agent_id == agent_id, m.Trigger.chat_session_id.is_(None))
    if hard_delete:
        await db.execute(delete(m.Trigger).where(*own_triggers))
        return
    await db.execute(
        update(m.Trigger)
        .where(*own_triggers, m.Trigger.enabled.is_(True))
        .values(enabled=False, last_skip_reason=AGENT_ARCHIVED)
    )
    open_runs = (
        (
            await db.execute(
                select(m.AgentRun).where(
                    m.AgentRun.agent_id == agent_id,
                    m.AgentRun.state.notin_([s.value for s in TERMINAL]),
                )
            )
        )
        .scalars()
        .all()
    )
    for run in open_runs:
        await record_operator_cancel(
            db,
            run=run,
            principal=principal,
            actor_id=None,
            resource_extra={"reason": AGENT_ARCHIVED, **(resource_extra or {})},
        )
