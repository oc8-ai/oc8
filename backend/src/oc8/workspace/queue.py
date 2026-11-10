"""The only legal way to read or answer a `Clarification` for a HUMAN (§0.B, §4).

The other half of the queue, and the half with no screen at all today. A run that
stopped to ask something sits in `waiting_for_input`, and the one way to answer it
is `POST /runs/{run_id}/answer`, gated on `run:control` -- a permission in no seat
vocabulary. So the person whose answer the agent is waiting for is precisely the
person who cannot give it. That admin door is left exactly as it is (§4); this is
the departmental one beside it.

**Where a clarification stands.** `Clarification.agent_id` is NOT NULL
(`models/run.py:71`) and `Agent.department_id` is NOT NULL, so the department is
one join away and the row needs no column of its own. That is not the same
decision as `approval_request.department_id`, which is DENORMALISED on purpose:
an approval outlives the state it was raised in and must not move when its agent
does, whereas a clarification is answered inside the run that asked it -- minutes,
not weeks -- and there is nothing to pin.

**No tenant-wide clarification exists.** Every one has an agent, so unlike an
approval there is no `department_id IS NULL` bucket here. A caller with an empty
scope therefore sees nothing at all, and that is the whole of the rule.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from oc8.models.core import Agent, Department
from oc8.models.run import AgentRun, Clarification
from oc8.runtime.clarification import resolve_clarification
from oc8.runtime.states import RunState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.scope import DecisionActor

#: Same ceiling as `approvals.repo`, and for the same reason: a list endpoint
#: with no ceiling is a denial of service with a friendly name.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


class NotAnswerable(Exception):
    """The question is yours, and it cannot be answered now.

    Distinct from `open_clarifications`/`answer_clarification` returning `None`,
    which means "no such question, or not yours" and must stay a 404: this one
    only ever escapes AFTER the scope has been checked, so it may say why. The
    route turns it into 409.
    """


@dataclass(frozen=True)
class ClarificationRow:
    """One open question, with everything the row on the screen needs.

    A projection rather than the ORM object, because the screen needs the agent's
    name and the department's, and the alternative is the endpoint issuing two
    more queries per row -- which is how a queue of forty questions becomes
    eighty-one round trips. `department_id` travels with it so the caller never
    has to resolve the department a second time (and never has to resolve it
    differently).
    """

    id: uuid.UUID
    run_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    department_id: uuid.UUID
    department_name: str
    question: str
    status: str
    created_at: dt.datetime
    #: `run.context["pending_questions"]` of the run that asked (spec §4.1).
    pending_questions: Any = None


async def open_clarifications(
    db: AsyncSession, *, actor: DecisionActor, limit: int = DEFAULT_LIMIT
) -> list[ClarificationRow]:
    """The open questions this actor may see, newest first.

    `actor` is keyword-only with no default for the same reason it is on
    `decide_approval` and `visible_approvals`: a caller that could omit it is a
    caller that lists the whole tenant, and nothing would fail.

    Newest first, matching `visible_approvals`. The screen merges the two into
    one queue (§7), and two halves sorted in opposite directions interleave into
    an order that is neither.

    The three-way branch below is the shape this module exists to make
    unwritable. `WHERE department_id IN ()` is not valid SQL, so "skip the filter
    when the set is empty" is the natural way to write it -- and it fails OPEN,
    handing a seatless employee every parked question in the company.
    """
    scope = actor.scope
    stmt = (
        select(
            Clarification.id,
            Clarification.run_id,
            Clarification.agent_id,
            Clarification.question,
            Clarification.status,
            Clarification.created_at,
            Agent.name,
            Agent.department_id,
            Department.name,
            AgentRun.context["pending_questions"],
        )
        # INNER on agent: the department comes from it, so a clarification whose
        # agent is unreadable here has no department and must not be shown to
        # anybody rather than shown to everybody. Agent rows are soft-deleted and
        # deliberately NOT filtered out: a question whose agent was archived
        # mid-run is still a parked run holding a task open, and hiding it would
        # make it both unanswerable and invisible.
        .join(Agent, Agent.id == Clarification.agent_id)
        # LEFT on department: the name is decoration, and a renamed-away or
        # archived department must not delete the question from the queue.
        .outerjoin(Department, Department.id == Agent.department_id)
        .outerjoin(AgentRun, AgentRun.id == Clarification.run_id)
        .where(Clarification.status == "open")
    )

    if scope.is_unrestricted:
        # No department predicate at all -- `viewable` is empty for an
        # unrestricted caller by design, so a filter built from it shows a CEO
        # nothing.
        pass
    elif scope.viewable:
        stmt = stmt.where(Agent.department_id.in_(scope.viewable))
    else:
        return []

    # `id` breaks the tie, and it is not decoration: `created_at` defaults to
    # `now()`, which in Postgres is TRANSACTION start time -- two questions asked
    # by the same run, or written by one migration, share it to the microsecond
    # and their relative order is then whatever the plan happens to emit. Ids are
    # uuid7 and time-ordered, so this is the same "newest" by a finer clock.
    stmt = stmt.order_by(Clarification.created_at.desc(), Clarification.id.desc()).limit(
        max(1, min(limit, MAX_LIMIT))
    )
    return [
        ClarificationRow(
            id=row[0],
            run_id=row[1],
            agent_id=row[2],
            question=row[3],
            status=row[4],
            created_at=row[5],
            agent_name=row[6],
            department_id=row[7],
            department_name=row[8] or "",
            pending_questions=row[9],
        )
        for row in (await db.execute(stmt)).all()
    ]


async def answer_clarification(
    db: AsyncSession, *, actor: DecisionActor, clarification_id: uuid.UUID, answer: str
) -> AgentRun | None:
    """Answer one parked question and re-queue the run that asked it.

    Returns the run, so the caller can commit and only THEN `publish_run` -- a
    stream entry whose run row is not yet visible is a run a worker picks up and
    cannot find. Nothing is committed here: a commit inside a `tenant_session`
    unbinds `app.tenant_id` for every statement after it, which in a route body
    is silent rather than loud.

    Returns `None` for BOTH "no such clarification" and "not in your
    departments", and the route 404s identically for the two. Same oracle guard
    as `approvals.repo.load_for_actor`: an id that answers differently is an id
    somebody can test.

    Raises `NotAnswerable` only once the scope has already admitted the caller.
    """
    row = (
        await db.execute(
            select(Clarification)
            .where(Clarification.id == clarification_id)
            # An explicit select and never `db.get`: `Session.get` returns an
            # already-loaded instance out of the identity map WITHOUT issuing
            # SQL, and this load is a security boundary whose answer has to come
            # from the database under this transaction's RLS binding.
            .execution_options(populate_existing=True)
            # Locks the CLARIFICATION row and deliberately not the run: a running
            # executor holds `agent_run`'s row lock for the whole run (see
            # `api/v1/run.py::cancel_run`), so locking the run would block an HTTP
            # request for as long as an agent takes. Nothing holds a clarification
            # row for longer than one short transaction. Two tabs answering the
            # same question then serialise, and the second re-reads it as already
            # answered instead of folding a second answer into the same run.
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    department_id = await db.scalar(select(Agent.department_id).where(Agent.id == row.agent_id))
    # `may_decide`, not `may_view`: `clarification:answer` is the decide-level
    # half of the seat vocabulary (`SEAT_PERMISSIONS`), so a `dept_viewer` reads
    # this queue and cannot empty it.
    #
    # And `department_id is None` is refused outright rather than handed to
    # `may_decide`, which reads NULL as TENANT-WIDE and would let an unrestricted
    # caller through. There is no tenant-wide clarification: `Agent.department_id`
    # is NOT NULL, so NULL here can only mean the agent row is not readable in
    # this tenant -- and `open_clarifications`' inner join has already left that
    # question out of every queue in the system. A row nothing lists and somebody
    # can still answer is the two doors disagreeing about which rows exist.
    if department_id is None or not actor.scope.may_decide(department_id):
        return None

    if row.status != "open":
        # Not merely redundant with the run-state check below. `resolve_clarification`
        # answers every OPEN clarification on the run, so re-answering a question
        # that is already closed would, on a run that has since parked on a
        # SECOND question, silently answer that newer one with this older answer.
        raise NotAnswerable("this question has already been answered")

    run = (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.id == row.run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:  # pragma: no cover - clarification.run_id is NOT NULL
        return None
    if run.state != RunState.WAITING_FOR_INPUT.value:
        # Same 409 the admin door gives (`api/v1/run.py:112`). A run that has
        # been cancelled or has failed since the question was asked cannot be
        # resumed, and recording the answer anyway would tell the person their
        # sentence reached an agent that will never read it.
        raise NotAnswerable("run is not waiting for input")

    # The one shared implementation, so the departmental door and the admin door
    # cannot fold the answer into `run.context` in two different shapes -- the
    # executor reads exactly one of them.
    pending = (run.context or {}).get("pending_questions")
    structured = await resolve_clarification(db, run=run, answer=answer)
    # A chat run's question answered from the inbox still shows in its chat as
    # the answered card (spec §4.1) -- the same turn every door writes.
    from oc8.authz.scope import HumanActor
    from oc8.chat.service import record_answer_turn

    principal = actor.principal if isinstance(actor, HumanActor) else None
    await record_answer_turn(
        db,
        run=run,
        question=row.question,
        pending=pending,
        structured=structured,
        text=answer,
        author_member_id=actor.member.id,
        acting_subject=principal.subject if principal is not None else None,
        acting_role=principal.role if principal is not None else None,
    )

    # WHO answered. There was no trail at all: `Clarification` has no
    # `answered_by` column and neither this door nor `POST /runs/{id}/answer`
    # wrote an audit event, so a sentence that went into a live run and changed
    # what an agent did next was attributable to nobody. `decide_approval` records
    # `decided_by` plus a `member_id`/`department_id` resource for exactly this
    # reason, and this slice is what makes unblocking a run reachable by somebody
    # who is not an administrator.
    #
    # The answer TEXT is deliberately not in the resource: it is the person's own
    # words, it is already on the clarification row, and the audit trail is read
    # by roles (`auditor`) that hold no seat in this department.
    from oc8.audit import append_event
    from oc8.authz.scope import HumanActor

    await append_event(
        db,
        tenant_id=row.tenant_id,
        actor_type="operator",
        actor_id=actor.member.id,
        category="run",
        action="clarification.answered",
        resource={
            "clarification_id": str(row.id),
            "run_id": str(run.id),
            "agent_id": str(row.agent_id),
            "department_id": str(department_id),
            "member_id": str(actor.member.id),
            "via": actor.via,
        },
        # Only an inbox answer has a token behind it; a `ChannelActor` is a
        # messenger message, which is not an authentication event. Same rule as
        # `decide_approval`, and the reason a binding with `member_id IS NULL`
        # answers nothing.
        principal=actor.principal if isinstance(actor, HumanActor) else None,
    )
    return run
