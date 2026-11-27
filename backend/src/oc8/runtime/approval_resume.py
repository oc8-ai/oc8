"""Human-loop for a value-gated tool call: an operator's approve/reject on a
suspended run is recorded in the run context and the run is re-queued, so the
worker resumes it and either executes the approved action or skips the rejected
one. Mirrors the clarification (ask_user) resume, one layer up for tool calls.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.approvals.repo import department_after_person
from oc8.models.identity import OrgMember
from oc8.models.ops import ApprovalRequest
from oc8.models.run import AgentRun
from oc8.runtime.fencing import one_line
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_context import append_consumed_approval, append_resolved_approval
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


async def open_parking_cards(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    actions: Sequence[str],
    skip_locked: bool = False,
) -> list[ApprovalRequest]:
    """The task's pending cards of `actions`, locked FOR UPDATE: what a run of
    the task may be parked on. For a cancel waking the run (cancellation.py);
    no person reads these, so the department scope does not apply."""
    return list(
        (
            await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.task_id == task_id,
                    ApprovalRequest.status == "pending",
                    ApprovalRequest.action_type.in_(list(actions)),
                )
                .with_for_update(skip_locked=skip_locked)
            )
        )
        .scalars()
        .all()
    )


async def run_of_approval(db: AsyncSession, approval: ApprovalRequest) -> AgentRun | None:
    """The run that raised `approval`, or None.

    Every turn of an agent chat or a Copilot conversation runs on ONE task, so
    "the run of this task" is ambiguous from the second turn on: every raiser
    names its run in the payload, and that run must be of this task. A card
    raised before raisers did so (still pending across an upgrade) falls back
    to the task's latest run started before the card -- turns of one task run
    one after another, so that is the one that raised it."""
    if approval.task_id is None:
        return None
    raw_run = (approval.payload or {}).get("run_id")
    if raw_run:
        try:
            run = await db.get(AgentRun, uuid.UUID(str(raw_run)))
        except ValueError:
            return None
        return run if run is not None and run.task_id == approval.task_id else None
    query = select(AgentRun).where(AgentRun.task_id == approval.task_id)
    if approval.created_at is not None:
        query = query.where(AgentRun.created_at <= approval.created_at)
    return (
        await db.execute(query.order_by(AgentRun.created_at.desc()).limit(1))
    ).scalar_one_or_none()


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
    run = await run_of_approval(db, approval)
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
            # Names the approval, so the call that uses it up can say which
            # one it used (`claim_approval`).
            "approval_id": str(approval.id),
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


def pre_decided_map(entries: list[dict[str, Any]]) -> dict[str, str]:
    """signature -> "approve"|"reject", for the engine to honour on replay."""
    return {e["sig"]: e["decision"] for e in entries if e.get("sig") and e.get("decision")}


def resolved_entries(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """signature -> its latest resolved entry (`decision`, and a rejection's
    `reason` and `decided_by_name` for `rejection_text`)."""
    return {e["sig"]: e for e in entries if e.get("sig") and e.get("decision")}


#: Prefix of the delivery marker an approval leaves when its call runs.
APPROVAL_MARKER = "approval#"


class Decisions:
    """An operator's decisions on held calls, as a run may still use them.

    One approval covers exactly ONE execution of the call it approved (E2E N1,
    security review): read from `resolved_tool_approvals` minus what
    `consumed_tool_approvals` already used up, so an identical repeat in the
    same run is a new call -- asked again, or guarded when it runs
    unattended. Two approvals of the same call cover two executions. A
    rejection stays: it is never used up.

    Every path that honours a decision reads it through here -- the engine,
    the MCP gateway, the isolated runtime's /tool and /tools, its local-tool
    pre-verdict and a held call's re-run -- and claims the approval BEFORE
    the call runs, through `claim_approval`. Behaves as the
    `signature -> verdict` mapping those paths used before.
    """

    def __init__(self, context: Mapping[str, Any] | None) -> None:
        ctx = context or {}
        entries = [e for e in ctx.get("resolved_tool_approvals") or [] if isinstance(e, dict)]
        latest = pre_decided_map(entries)
        self.decided = bool(latest)
        #: signature -> its latest resolved entry, for what a rejection tells
        #: the model (`rejection_text`).
        self.latest: dict[str, dict[str, Any]] = resolved_entries(entries)
        consumed = [c for c in ctx.get("consumed_tool_approvals") or [] if isinstance(c, dict)]
        used = {str(c.get("key")) for c in consumed}
        #: call id -> (its signature, the approval claimed for it before it
        #: ran). Bound to the signature: a call id is the model's, and a
        #: different call reusing it must never ride on the claim.
        self.claimed_calls: dict[str, tuple[str, str]] = {
            str(c["call_id"]): (str(c.get("sig")), str(c.get("key")))
            for c in consumed
            if c.get("call_id")
        }
        #: approval key -> its entry, for what the approval carries.
        self.entries: dict[str, dict[str, Any]] = {}
        #: signature -> the keys of its approvals not yet used up, in order.
        self.open: dict[str, list[str]] = {}
        seen: dict[str, int] = {}
        for e in entries:
            sig = e.get("sig")
            if not sig or e.get("decision") != "approve":
                continue
            seen[sig] = seen.get(sig, 0) + 1
            key = str(e.get("approval_id") or f"{sig}#{seen[sig]}")
            self.entries[key] = e
            if key not in used:
                self.open.setdefault(sig, []).append(key)
        self.verdicts: dict[str, str] = {
            sig: verdict
            for sig, verdict in latest.items()
            if verdict == "reject" or self.open.get(sig)
        }

    @classmethod
    def of(
        cls, decided: Decisions | Mapping[str, str | Mapping[str, Any]] | None
    ) -> Decisions | None:
        """A plain `signature -> verdict` map, or `signature -> resolved entry`
        (`resolved_entries`), as unused decisions."""
        if decided is None or isinstance(decided, Decisions):
            return decided
        return cls(
            {
                "resolved_tool_approvals": [
                    {**v, "sig": k} if isinstance(v, Mapping) else {"sig": k, "decision": v}
                    for k, v in decided.items()
                ]
            }
        )

    def entry(self, sig: str) -> dict[str, Any]:
        """The latest resolved entry of this call, `{}` when nobody decided it."""
        return self.latest.get(sig) or {}

    def get(self, sig: str, default: str | None = None) -> str | None:
        return self.verdicts.get(sig, default)

    def __bool__(self) -> bool:
        """Whether anybody decided anything for this run -- a resume leg --
        even if every approval is used up by now."""
        return self.decided

    def approved(self, sig: str) -> bool:
        return self.verdicts.get(sig) == "approve"

    def available(self, sig: str) -> int:
        """How many executions of this call the open approvals still cover."""
        return len(self.open.get(sig) or []) if self.approved(sig) else 0

    def discard(self, sig: str, key: str) -> None:
        """Forget one approval found used up elsewhere."""
        keys = self.open.get(sig) or []
        if key in keys:
            keys.remove(key)
        if not keys:
            self.open.pop(sig, None)
            if self.verdicts.get(sig) == "approve":
                self.verdicts.pop(sig, None)

    def claimed_call(self, call_id: str, sig: str) -> str | None:
        """The approval claimed for this very call -- same id AND same
        signature -- before it ran, if any."""
        mine = self.claimed_calls.get(call_id)
        return mine[1] if mine is not None and mine[0] == sig else None

    def drop(self, sig: str) -> str | None:
        """Forget the next open approval of this call; its key."""
        keys = self.open.get(sig)
        if not keys:
            return None
        key = keys.pop(0)
        if not keys:
            self.open.pop(sig, None)
            if self.verdicts.get(sig) == "approve":
                self.verdicts.pop(sig, None)
        return key


async def live_decisions(
    db: AsyncSession,
    *,
    context: Mapping[str, Any] | None,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID | None,
) -> Decisions:
    """`Decisions` of a run, also dropping an approval whose call already
    ran by its delivery marker -- which lives outside the run context, so a
    leg redelivered after a crash, or a context written back from an older
    copy, cannot make one approval send twice."""
    decisions = Decisions(context)
    if task_id is None:
        return decisions
    from oc8.agent.outward import already_delivered

    for sig, keys in list(decisions.open.items()):
        for key in list(keys):
            if await already_delivered(
                db, tenant_id=tenant_id, task_id=task_id, target=APPROVAL_MARKER + key
            ):
                decisions.discard(sig, key)
    return decisions


async def drop_used(db: AsyncSession, run: AgentRun | None, decisions: Decisions, sig: str) -> None:
    """Forget the open approvals of this call whose delivery marker says a
    call already used them -- what `claim_approval` would skip -- so a caller
    deciding now, and claiming only at dispatch, counts what is truly left."""
    if run is None or run.task_id is None:
        return
    from oc8.agent.outward import already_delivered

    for key in list(decisions.open.get(sig) or []):
        if await already_delivered(
            db, tenant_id=run.tenant_id, task_id=run.task_id, target=APPROVAL_MARKER + key
        ):
            decisions.discard(sig, key)


async def claim_approval(
    db: AsyncSession,
    run: AgentRun | None,
    decisions: Decisions,
    sig: str,
    *,
    call_id: str | None = None,
    ctx: dict[str, Any] | None = None,
) -> str | None:
    """Claim one unused approval of this call BEFORE the call runs; its key,
    or None when none is left -- then the call is asked again (or refused
    when the run is unattended), never run on a used approval.

    The claim is the approval's delivery marker, inserted
    `ON CONFLICT DO NOTHING RETURNING` (`claim_delivery`): of two requests
    racing on one approval exactly one wins, the other waits for its
    transaction and moves on to the next open approval, if any. The use is
    also appended to the run context (`consumed_tool_approvals`, with the
    `call_id` it was claimed for). A claim is kept when the call then fails:
    the far system may have acted first.

    At-least-once at the edge: the claim commits with the caller's
    transaction. A crash after the call reached the far system but before
    that commit loses the claim, and the redelivered request runs the call
    again -- the window every send-then-record system has; the outward and
    replay records narrow it, nothing here closes it.

    `ctx`: a copy of the context the caller writes back whole, kept in step.
    Without a run (an in-process call with no row) the approval is only
    forgotten in this process."""
    from oc8.agent.outward import claim_delivery

    while decisions.approved(sig):
        key = decisions.drop(sig)
        if key is None:
            return None
        if run is None:
            return key
        if run.task_id is not None and not await claim_delivery(
            db, tenant_id=run.tenant_id, task_id=run.task_id, target=APPROVAL_MARKER + key
        ):
            continue  # another request used this one; try the next
        entry: dict[str, Any] = {"sig": sig, "key": key}
        if call_id is not None:
            entry["call_id"] = call_id
            decisions.claimed_calls[call_id] = (sig, key)
        await append_consumed_approval(db, run, entry)
        if ctx is not None:
            ctx["consumed_tool_approvals"] = [*(ctx.get("consumed_tool_approvals") or []), entry]
        return key
    return None
