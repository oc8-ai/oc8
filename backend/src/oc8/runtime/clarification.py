"""Human-loop: suspend a run on an open question; resume when answered."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.harness.stages.c_ledger import mark_decision_answered
from oc8.agent.harness.state import CONTEXT_KEY, HarnessState
from oc8.models.run import AgentRun, Clarification
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState


class RepeatedClarification(RuntimeError):
    """Raised instead of parking again when a run re-asks a question it has
    already been given an answer to (verbatim) on this same run.

    Every clarification the agent asks about SHOULD reach it as a fresh
    turn on its next leg (`executor.py` appends the full Q&A history to
    `task_text`) -- confirmed by hand against the real Claude Code CLI: given
    that exact resumed prompt, it used the answer and moved on. So an agent
    that asks the identical question again did not fail to receive the
    answer through any path this system controls; something -- a runtime's
    own resume mechanics under real timing, a non-deterministic model retry,
    a CLI quirk -- kept it from actually being incorporated, and no amount of
    inspecting oc8's own code changes that outcome from here.

    What IS this system's job is to never let that failure mode become an
    operator's problem: without this check, a human who answers once just
    gets asked the same thing again, and again, each time believing they
    are one step from unblocking a stuck agent. The first ask always parks
    normally (an honest, non-repeating question is unaffected); the FIRST
    verbatim repeat of it fails the run outright, loud and specific, rather
    than parking the human a second time on a question already answered."""


async def request_clarification(db: AsyncSession, *, run: AgentRun, question: str) -> Clarification:
    prior_answers = [
        c.get("answer", "")
        for c in run.context.get("clarifications", [])
        if c.get("question") == question
    ]
    if prior_answers:
        raise RepeatedClarification(
            "The agent asked this exact question again after already receiving an "
            f"answer to it ({len(prior_answers)} time(s) before): {question!r}. "
            "Stopping instead of parking a second time on the same question."
        )
    clar = Clarification(
        tenant_id=run.tenant_id,
        run_id=run.id,
        agent_id=run.agent_id,
        question=question,
        status="open",
    )
    db.add(clar)
    await RunRepository(db).transition(run, RunState.WAITING_FOR_INPUT)
    await db.flush()
    return clar


async def resolve_clarification(db: AsyncSession, *, run: AgentRun, answer: str) -> None:
    """Answer the run's open clarification(s), fold Q&A into `run.context`, and
    re-queue the run so the worker resumes it with the answer visible."""
    rows = (
        (
            await db.execute(
                select(Clarification).where(
                    Clarification.run_id == run.id, Clarification.status == "open"
                )
            )
        )
        .scalars()
        .all()
    )
    entries = list(run.context.get("clarifications", []))
    for clar in rows:
        clar.answer = answer
        clar.status = "answered"
        entries.append({"question": clar.question, "answer": answer})
    # Mark the ledger decision answered while the harness is still on the
    # current context, before we replace it (Package 9 ruling 6).
    if CONTEXT_KEY in run.context:
        harness = HarnessState.from_run_context(run.context)
        mark_decision_answered(harness.ledger)
        harness.store(run.context)
    new_ctx = {**run.context, "clarifications": entries}
    new_ctx.pop("pending_question", None)
    run.context = new_ctx
    followup = (run.context or {}).get("followup")
    if run.source == "chat" and isinstance(followup, dict) and followup.get("responsibility_id"):
        resp = await db.get(m.Responsibility, uuid.UUID(str(followup["responsibility_id"])))
        if resp is not None and resp.state == "waiting":
            resp.state = "active"
    await RunRepository(db).transition(run, RunState.QUEUED)
    await db.flush()
