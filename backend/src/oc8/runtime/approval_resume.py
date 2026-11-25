"""Human-loop for a value-gated tool call: an operator's approve/reject on a
suspended run is recorded in the run context and the run is re-queued, so the
worker resumes it and either executes the approved action or skips the rejected
one. Mirrors the clarification (ask_user) resume, one layer up for tool calls.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.approvals.repo import department_after_person
from oc8.models.identity import OrgMember
from oc8.models.ops import ApprovalRequest
from oc8.models.run import AgentRun
from oc8.runtime.fencing import one_line
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_context import append_resolved_approval
from oc8.runtime.states import RunState

#: How long a rejection reason may be when the model is told it.
REASON_LIMIT: Final = 500
#: Who rejected a call, when the decider has no name the model may be told.
NO_DECIDER: Final = "a reviewer"
#: What the model is told about a rejected call that carries no reason.
REJECTED: Final = "operator rejected this action"


def call_signature(tool: str, arguments: dict[str, Any]) -> str:
    """Same signature the engine computes, so a decision made here matches the
    call when the run replays it."""
    return tool + "\n" + json.dumps(arguments, sort_keys=True, default=str)


async def _decider_name(db: AsyncSession, approval: ApprovalRequest) -> str:
    """The decider's display name, as the approval card shows it; "" when
    there is none. A name that is an email address is not told to a model."""
    if approval.decided_by is None:
        return ""
    member = await db.get(OrgMember, approval.decided_by)
    name = one_line(member.display_name or "", 120) if member is not None else ""
    return "" if "@" in name else name


async def resolve_tool_approval(
    db: AsyncSession, *, approval: ApprovalRequest, decision: str
) -> AgentRun | None:
    """Record a tool_send approval decision on its run and re-queue the run.

    Returns the run to publish, or None if no run can be resumed (e.g. the run
    already moved on). `decision` is "approve" or "reject".
    """
    if approval.task_id is None:
        return None
    if decision == "approve" and department_after_person(approval) is not None:
        # The person's approval of a write a department's policy holds is
        # never the call's approval: the department's card that it raises
        # (decide_approval) is the one that resumes the run (spec §1).
        return None
    # A Copilot conversation runs every turn on ONE task, so "the run of this
    # task" is ambiguous there (MultipleResultsFound from the second turn on):
    # a raiser that knows its run says which, and it must be of this task.
    raw_run = (approval.payload or {}).get("run_id")
    run: AgentRun | None
    if raw_run:
        try:
            run = await db.get(AgentRun, uuid.UUID(str(raw_run)))
        except ValueError:
            run = None
        if run is not None and run.task_id != approval.task_id:
            run = None
    else:
        run = (
            await db.execute(select(AgentRun).where(AgentRun.task_id == approval.task_id))
        ).scalar_one_or_none()
    if run is None:
        return None

    tool = str(approval.payload.get("tool", ""))
    arguments = approval.payload.get("arguments") or {}
    # Recorded FIRST and unconditionally. A long-lived runtime driving tools
    # through the MCP gateway is still RUNNING -- nothing exits, so nothing moves
    # the run to waiting_for_approval -- and it will retry the held call itself.
    # Bailing out before this (as this function used to) dropped the operator's
    # decision on the floor: the retry raised a second approval, and a human could
    # approve forever without the agent ever proceeding.
    #
    # Appended in ONE statement rather than read-merge-written here: the run is
    # still executing, so the executor is about to write its own keys to the same
    # JSONB column from a snapshot that predates this decision. See run_context.
    await append_resolved_approval(
        db,
        run,
        {
            "sig": call_signature(tool, arguments),
            "tool": tool,
            "arguments": arguments,
            "decision": decision,
            # What the model is told about a rejection (`rejection_text`).
            "reason": one_line(approval.reason or "", REASON_LIMIT),
            "decided_by_name": await _decider_name(db, approval),
            # Only a raiser that saw the call pass read-before-write sets it.
            **(
                {
                    "read_before_write_ok": True,
                    "gate_connection": str(approval.payload.get("gate_connection") or ""),
                }
                if approval.payload.get("read_before_write_ok") is True
                else {}
            ),
        },
    )
    await db.flush()

    # Closing half of the parking race: if the executor's own transition to
    # WAITING_FOR_APPROVAL is in flight concurrently, the UPDATE above blocks on
    # its row lock until that transition commits -- so by the time this line
    # runs, the run may already be parked even though it was still RUNNING when
    # `run` was first loaded above. Re-read rather than trust the in-memory copy,
    # or a decision landing in exactly that sub-window reads a stale "running"
    # and is declined here, forever (the executor's requeue_if_already_decided
    # covers the OTHER half: a decision already committed before the run parks).
    # populate_existing forces this to hit the database instead of returning the
    # identity-mapped (and now stale) object.
    run = await db.get(AgentRun, run.id, populate_existing=True)
    if run is None or run.state != RunState.WAITING_FOR_APPROVAL.value:
        # Decision recorded, but there is nothing to wake: either a live runtime
        # will pick it up on its next attempt, or the run has already moved on.
        return None

    await RunRepository(db).transition(run, RunState.QUEUED)
    await db.flush()
    return run


def rejection_text(entry: Mapping[str, Any]) -> str:
    """What the model is told about a call a person rejected: who and why,
    when they gave a reason, so it changes course instead of retrying."""
    reason = one_line(str(entry.get("reason") or ""), REASON_LIMIT)
    if not reason:
        return REJECTED
    name = one_line(str(entry.get("decided_by_name") or ""), 120) or NO_DECIDER
    return f"rejected by {name}: {reason}. Adjust your approach instead of retrying the same call."


def resume_instruction(entries: list[dict[str, Any]]) -> str:
    """A focused instruction for the resumed leg: perform exactly the
    approved calls (so the model reproduces them and the engine's pre-decision
    executes them), and do not perform the rejected ones. A run without a
    conversation gets it in place of its task, so the model does not replay
    earlier read/write steps; a chat turn of the in-process engine gets it
    after its conversation (`agent.preamble.Resume`)."""
    approved = [e for e in entries if e.get("decision") == "approve"]
    rejected = [e for e in entries if e.get("decision") == "reject"]
    lines: list[str] = [
        "Resuming after a decision. A person decided one or more actions. Do NOT "
        "repeat earlier steps (no new searches, no new notes)."
    ]
    for e in approved:
        lines.append(
            "APPROVED — carry out exactly this action now: "
            f"{e['tool']} with {json.dumps(e['arguments'], ensure_ascii=False)}"
        )
    for e in rejected:
        line = (
            "REJECTED — do NOT carry out this action: "
            f"{e['tool']} with {json.dumps(e['arguments'], ensure_ascii=False)}"
        )
        why = rejection_text(e)
        lines.append(line if why == REJECTED else f"{line} — {why}")
    lines.append("Then sum up in 1-2 sentences and call no further tools.")
    return "\n".join(lines)


def pre_decided_map(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """signature -> its resolved entry (`decision` "approve"|"reject", and a
    rejection's `reason` for `rejection_text`), for the engine to honour on
    replay."""
    return {e["sig"]: e for e in entries if e.get("sig") and e.get("decision")}
