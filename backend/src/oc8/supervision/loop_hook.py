"""Checkpoint-emission hook for run_agent (§8.6 / §8.3 step 5a). Wires
TaskAnchor creation and cadence-driven checkpoint emission into the agent
execution loop for supervised agents. Never raises out of run_agent — a
missing supervision assignment or a broken/missing policy degrades to "skip",
it never interrupts the agent's actual task.

Since §8.6 Task 3 this hook also runs the tier-0 watchdogs, the sampled tier-2
judge and the intervention ladder inline. Each runs in its own savepoint-guarded
try block so that NOTHING escapes maybe_checkpoint and nothing poisons the
caller's session: a judge failure still lets the ladder run on the tier-1 score,
a ladder failure degrades to "no intervention", and in both cases the checkpoint
is still returned and the session stays usable. Only `steer` and
`pause_escalate` may be applied automatically (AUTO_APPLY_RUNGS) — `rewind`
needs a context-snapshot mechanism that doesn't exist yet, and `reassign` is
operator-driven, so both stay manual/API-triggered."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.modelrouter import EmbeddingUnavailable, get_model_router
from oc8.supervision.checkpoints import evaluate_and_checkpoint
from oc8.supervision.detection import loop_detected, token_burn_alert, tool_error_alert
from oc8.supervision.intervention import supervise_checkpoint
from oc8.supervision.judge import VERDICT_FLOOR, judge_checkpoint, should_judge
from oc8.supervision.service import create_task_anchor, get_supervisor

_STATE_SUMMARY_MAX_CHARS = 2000
_FALLBACK_SUMMARY = "(no activity since last checkpoint)"

# The only rungs a supervisor may apply WITHOUT a human in the loop. `rewind`
# would need a context-snapshot/restore mechanism that does not exist, and
# `reassign` is operator-driven; both remain API-triggered only.
AUTO_APPLY_RUNGS = ("steer", "pause_escalate")

# The tier-0 token watchdog must NOT reuse the checkpoint cadence as its budget:
# a token-cadence checkpoint only fires once tokens_since_checkpoint >= the
# cadence limit, so a budget equal to the cadence would be true by construction
# at every single checkpoint -- forcing a judge call even at sampling_rate=0.0.
# The watchdog is for genuine burn, so it triggers only well beyond one interval.
_TIER0_TOKEN_BUDGET_FACTOR = 4


def _tier0_alert(
    tool_trace_delta: list[dict[str, Any]],
    tokens_since_checkpoint: int,
    policy: m.SupervisionPolicy,
) -> bool:
    """Tier-0 watchdogs, computed from data the loop already hands us."""
    names = [str(call.get("tool", "")) for call in tool_trace_delta]
    errors = sum(1 for call in tool_trace_delta if _is_error_call(call))
    cadence_tokens = int((policy.checkpoint_every or {}).get("tokens") or 0)
    # No token cadence configured -> the token watchdog is not evaluated at all.
    token_budget = cadence_tokens * _TIER0_TOKEN_BUDGET_FACTOR
    return (
        loop_detected(names)
        or (token_budget > 0 and token_burn_alert(tokens_since_checkpoint, budget=token_budget))
        or (len(names) > 0 and tool_error_alert(errors, len(names)))
    )


def _is_error_call(call: dict[str, Any]) -> bool:
    """A trace entry counts as an error if it carries an explicit flag, or if
    run_agent stringified the failure into the result (its actual convention:
    denied/failed tool calls come back as "ERROR: ...")."""
    if call.get("error"):
        return True
    return str(call.get("result", "")).startswith("ERROR:")


def _summarize_trace(trace_delta: list[dict[str, Any]]) -> str:
    lines = [
        f"{entry.get('tool', '?')}: {str(entry.get('result', entry.get('decision', '')))[:120]}"
        for entry in trace_delta
    ]
    return "\n".join(lines)[:_STATE_SUMMARY_MAX_CHARS]


async def _embed(text: str) -> list[float] | None:
    try:
        return await get_model_router().embed(text)
    except EmbeddingUnavailable:
        return None


async def maybe_create_anchor(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    task_text: str,
) -> m.TaskAnchor | None:
    """Create a TaskAnchor for this task iff the agent has a supervisor
    assignment. Returns None for unsupervised agents — no anchor, and no
    checkpoints will be emitted for this run."""
    assignment = await get_supervisor(db, supervised_agent_id=agent_id)
    if assignment is None:
        return None
    embedding = await _embed(task_text)
    return await create_task_anchor(
        db,
        tenant_id=tenant_id,
        task_id=task_id,
        objective=task_text,
        acceptance_criteria={},
        constraints={},
        anchor_embedding=embedding,
    )


async def maybe_checkpoint(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    anchor: m.TaskAnchor | None,
    tool_trace_delta: list[dict[str, Any]],
    tokens_since_checkpoint: int,
    force: bool,
    contains_restricted: bool = False,
) -> m.AgentCheckpoint | None:
    """Emit a checkpoint if the department's checkpoint cadence is met (or
    force=True). anchor is None means unsupervised — the only condition
    under which no checkpoint is ever emitted, forced or not."""
    if anchor is None:
        return None
    assignment = await get_supervisor(db, supervised_agent_id=agent_id)
    if assignment is None:
        return None
    policy = await db.get(m.SupervisionPolicy, assignment.policy_id)
    if policy is None:
        return None
    cadence = policy.checkpoint_every or {}
    tool_call_limit = cadence.get("tool_calls")
    token_limit = cadence.get("tokens")
    threshold_met = (
        tool_call_limit is not None and len(tool_trace_delta) >= int(tool_call_limit)
    ) or (token_limit is not None and tokens_since_checkpoint >= int(token_limit))
    if not force and not threshold_met:
        return None
    state_summary = _summarize_trace(tool_trace_delta) or _FALLBACK_SUMMARY
    trajectory_embedding = await _embed(state_summary)
    checkpoint = await evaluate_and_checkpoint(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        task_id=task_id,
        state_summary=state_summary,
        anchor=anchor,
        thresholds=dict(policy.drift_thresholds or {}),
        trajectory_embedding=trajectory_embedding,
    )
    tier0 = _tier0_alert(tool_trace_delta, tokens_since_checkpoint, policy)
    # Two INDEPENDENT best-effort blocks. Judging and the ladder each get their
    # own savepoint so (a) a failure while judging still lets the ladder run on
    # the tier-1 score, and (b) a DB-level failure rolls back to the savepoint
    # instead of leaving the caller's AsyncSession in PendingRollbackError state
    # — supervision must never break the supervised run, not even one statement
    # later in run_agent.
    try:
        async with db.begin_nested():
            await _judge(
                db,
                tenant_id=tenant_id,
                checkpoint=checkpoint,
                anchor=anchor,
                assignment=assignment,
                policy=policy,
                state_summary=state_summary,
                tier0=tier0,
                contains_restricted=contains_restricted,
            )
    except Exception:
        pass
    try:
        async with db.begin_nested():
            await _intervene(db, checkpoint=checkpoint, assignment=assignment, policy=policy)
    except Exception:
        pass
    return checkpoint


async def _judge(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    checkpoint: m.AgentCheckpoint,
    anchor: m.TaskAnchor,
    assignment: m.SupervisionAssignment,
    policy: m.SupervisionPolicy,
    state_summary: str,
    tier0: bool,
    contains_restricted: bool,
) -> None:
    """Sampled tier-2 judge; records its verdict on the checkpoint.

    Best-effort — the caller runs this inside a savepoint and swallows anything
    raised here, so the intervention ladder still runs on the tier-1 score."""
    if not should_judge(policy=policy, tier1_verdict=checkpoint.verdict, tier0_alert=tier0):
        return
    verdict = await judge_checkpoint(
        db,
        tenant_id=tenant_id,
        policy=policy,
        anchor=anchor,
        checkpoint=checkpoint,
        trajectory_excerpt=state_summary,
        signals={
            "drift_score": (
                float(checkpoint.drift_score) if checkpoint.drift_score is not None else None
            ),
            "tier1_verdict": checkpoint.verdict,
            "tier0_alert": tier0,
        },
        contains_restricted=contains_restricted,
        supervisor_agent_id=assignment.supervisor_agent_id,
    )
    if verdict is not None:
        checkpoint.judge_verdict = verdict.verdict
        checkpoint.judge_evidence = {
            "evidence": verdict.evidence,
            "rationale": verdict.rationale,
            "recommended_intervention": verdict.recommended_intervention,
            "model": verdict.model,
            "forced": tier0 or checkpoint.verdict in ("drifting", "off_track"),
        }
        await db.flush()


async def _intervene(
    db: AsyncSession,
    *,
    checkpoint: m.AgentCheckpoint,
    assignment: m.SupervisionAssignment,
    policy: m.SupervisionPolicy,
) -> None:
    """The intervention ladder, on the tier-1 score floored by any judge verdict.

    Best-effort — see _judge."""
    tier1_score = float(checkpoint.drift_score) if checkpoint.drift_score is not None else 0.0
    effective = max(tier1_score, VERDICT_FLOOR.get(checkpoint.judge_verdict or "", 0.0))
    allowed = [str(x) for x in (policy.allowed_interventions or []) if x in AUTO_APPLY_RUNGS]
    await supervise_checkpoint(
        db,
        checkpoint=checkpoint,
        assignment=assignment,
        policy=policy,
        effective_score=effective,
        allowed_override=allowed,
    )
