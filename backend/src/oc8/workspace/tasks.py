"""The workspace's third queue, beside approvals and clarifications: what is
actually being worked on, across every department a member can see, plus the
one new way to put something on it (§14a.4, My Work task board).

`Task.state` is not something this module -- or anybody outside
`RunRepository.transition` -- ever sets by hand. A task is a live reflection of
the run underneath it (see `runtime/repository.py`), so "moving" a card between
columns here is not a write to `Task.state`; it is one of two things that are
already real: answering the run's question (`workspace/queue.py`), deciding its
approval (`approvals/repo.py`), or -- new here -- starting a fresh run at all.
There is no third write.

**Creating a task is starting a run, not inserting a row.** `create_task`
mirrors `collab/intake.py::route_to_team_lead` almost line for line: find the
department's team lead, open a task pinned to that lead, and hand
`enqueue_run` the task id so the engine does not open a second one when the
run picks it up. The one difference is honesty in the other direction --
`route_to_team_lead` accepts a headless department and leaves the handoff for
a human because inventing an assignee would hide that nobody is on it; a
member sitting at the board asking for a team lead that does not exist gets
told so immediately (`NoTeamLead`), because there is no human left to leave it
for.

**Attribution does not overload `created_by`.** That column already means "the
agent that opened this task" everywhere it is written
(`agent/engine.py`, `collab/intake.py`) and nothing reads it back yet -- adding
a second meaning to an unread column is how it becomes a landmine the day
something finally does read it. The member who asked for the work goes in
`payload["requested_by_member_id"]` instead, which is free-form by design and
already the place a task's origin lives.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from oc8.models.core import Agent, Department
from oc8.models.ops import Task
from oc8.runtime.intake import enqueue_run
from oc8.tenants.activity import TenantInactive

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.scope import DecisionActor

logger = logging.getLogger(__name__)

#: Same ceiling as `approvals.repo`/`workspace.queue`, and for the same reason.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

#: The 7 states `Task.state`'s `CheckConstraint` allows, collapsed onto the 4
#: columns a board renders. Lives here rather than in `api/v1/_serializers.py`
#: (which re-exports it for `task_to_dto`) because it is a fact about what a
#: task IS, not about how one is put on the wire.
TASK_STATE_TO_COLUMN = {
    "backlog": "backlog",
    "in_progress": "in_progress",
    "waiting_for_approval": "waiting",
    "waiting_for_input": "waiting",
    "budget_exceeded": "waiting",
    "done": "done",
    "failed": "done",
}


class NoTeamLead(Exception):
    """The department has nobody to hand the new task to."""


@dataclass(frozen=True)
class TaskRow:
    """One card on the board, with everything the screen needs -- see
    `workspace/queue.py::ClarificationRow` for why this is a projection and
    not the ORM row.
    """

    id: uuid.UUID
    title: str
    state: str
    column: str
    department_id: uuid.UUID
    department_name: str
    agent_id: uuid.UUID | None
    agent_name: str | None
    requested_by_member_id: uuid.UUID | None
    parent_task_id: uuid.UUID | None
    delegation_depth: int
    created_at: dt.datetime


def _requested_by(task: Task) -> uuid.UUID | None:
    raw = (task.payload or {}).get("requested_by_member_id")
    return uuid.UUID(raw) if raw else None


def _row(task: Task, agent_name: str | None, department_name: str | None) -> TaskRow:
    return TaskRow(
        id=task.id,
        title=task.title,
        state=task.state,
        column=TASK_STATE_TO_COLUMN.get(task.state, "backlog"),
        department_id=task.department_id,
        department_name=department_name or "",
        agent_id=task.assigned_agent_id,
        agent_name=agent_name,
        requested_by_member_id=_requested_by(task),
        parent_task_id=task.parent_task_id,
        delegation_depth=task.delegation_depth,
        created_at=task.created_at,
    )


async def visible_tasks(
    db: AsyncSession,
    *,
    actor: DecisionActor,
    department_id: uuid.UUID | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[TaskRow]:
    """The tasks this actor may see, newest first -- same three-way branch as
    `visible_approvals`/`open_clarifications`, and the same reason: `WHERE
    department_id IN ()` is not valid SQL, and skipping the filter for an empty
    set is how a seatless caller ends up seeing the whole tenant by accident.

    `department_id`, when given, INTERSECTS the scope rather than widening it:
    a department the actor cannot see returns `[]`, not a 403 that would
    confirm the department exists.
    """
    scope = actor.scope
    stmt = (
        select(Task, Agent.name, Department.name)
        .outerjoin(Agent, Agent.id == Task.assigned_agent_id)
        .outerjoin(Department, Department.id == Task.department_id)
    )

    if scope.is_unrestricted:
        pass
    elif scope.viewable:
        stmt = stmt.where(Task.department_id.in_(scope.viewable))
    else:
        return []

    if department_id is not None:
        if not scope.may_view(department_id):
            return []
        stmt = stmt.where(Task.department_id == department_id)

    stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(
        max(1, min(limit, MAX_LIMIT))
    )
    rows = (await db.execute(stmt)).all()
    return [_row(task, agent_name, department_name) for task, agent_name, department_name in rows]


async def create_task(
    db: AsyncSession,
    *,
    actor: DecisionActor,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID,
    member_id: uuid.UUID,
    instructions: str,
    title: str | None = None,
) -> TaskRow:
    """Put a new task on the target department's lead, on a member's own say-so.

    Raises `NoTeamLead` for a department with none -- unlike
    `route_to_team_lead`, there is no human on the other end of THIS call to
    leave it for; the member asking is the human, and they need to know now.
    """
    if not actor.scope.may_view(department_id):
        raise NoTeamLead(f"department {department_id} has no team lead to receive tasks")

    dept = await db.get(Department, department_id)
    if dept is None or dept.team_lead_agent_id is None:
        raise NoTeamLead(f"department {department_id} has no team lead to receive tasks")
    lead = await db.get(Agent, dept.team_lead_agent_id)
    if lead is None or lead.deleted_at is not None:
        raise NoTeamLead(f"department {department_id} has no team lead to receive tasks")

    task = Task(
        tenant_id=tenant_id,
        department_id=dept.id,
        assigned_agent_id=lead.id,
        title=title or instructions[:120],
        payload={"requested_by_member_id": str(member_id)},
        state="in_progress",
    )
    db.add(task)
    await db.flush()

    # `task_id` travels with the run at creation for the same reason
    # `route_to_team_lead` passes it: without it the engine opens a task of its
    # own when the run starts, and the board shows the same request twice.
    try:
        await enqueue_run(
            db,
            tenant_id=tenant_id,
            agent_id=lead.id,
            context={"task": instructions},
            source="manual",
            idempotency_key=f"member-task:{task.id}",
            task_id=task.id,
        )
    except TenantInactive:
        # Refused before anything was committed, so the tenant binding is
        # still in place: the task stays with the caller's transaction, but
        # no run starts on it.
        logger.info(
            "task %s created, but no run started: organisation %s is not active",
            task.id,
            tenant_id,
        )
        return _row(task, lead.name, dept.name)
    # `enqueue_run` COMMITS, which ends the GUC set by `tenant_session`. Anything
    # read after it -- the row this function returns -- would run unbound and
    # see nothing. Same fix, same reason, as `collab/intake.py`.
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
    )
    return _row(task, lead.name, dept.name)
