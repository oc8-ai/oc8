"""The graduated, audited intervention ladder (§8.6.3). A supervisor never
widens permissions; every intervention writes a row and an audit event."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.models.supervision import (
    AgentCheckpoint,
    SupervisionAssignment,
    SupervisionIntervention,
    SupervisionPolicy,
)

# Ascending severity. `reassign` is policy-gated / operator-driven, not part of
# the automatic score ladder.
_LADDER = ["steer", "rewind", "pause_escalate"]


def choose_intervention(
    score: float | None, thresholds: dict[str, Any], allowed: list[str]
) -> str | None:
    """Map a drift score to an intervention using the policy thresholds, then
    downgrade to the most severe *allowed* rung at or below it. None = on_track
    or nothing allowed."""
    if score is None:
        return None
    steer = float(thresholds.get("steer", 0.35))
    rewind = float(thresholds.get("rewind", 0.55))
    escalate = float(thresholds.get("escalate", 0.75))
    if score >= escalate:
        idx = 2
    elif score >= rewind:
        idx = 1
    elif score >= steer:
        idx = 0
    else:
        return None
    for i in range(idx, -1, -1):
        if _LADDER[i] in allowed:
            return _LADDER[i]
    return None


async def record_intervention(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    supervisor_agent_id: uuid.UUID,
    supervised_agent_id: uuid.UUID,
    task_id: uuid.UUID,
    kind: str,
    reason: dict[str, Any],
    checkpoint_id: uuid.UUID | None = None,
    outcome: str | None = None,
) -> SupervisionIntervention:
    interv = SupervisionIntervention(
        tenant_id=tenant_id,
        supervisor_agent_id=supervisor_agent_id,
        supervised_agent_id=supervised_agent_id,
        task_id=task_id,
        checkpoint_id=checkpoint_id,
        kind=kind,
        reason=reason,
        outcome=outcome,
    )
    db.add(interv)
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="agent",
        actor_id=supervisor_agent_id,
        category="supervision",
        action=f"intervention:{kind}",
        resource={"supervised_agent_id": str(supervised_agent_id), "task_id": str(task_id)},
        decision="applied",
        reason=str(reason),
    )
    await db.flush()

    from oc8.realtime.bus import get_event_bus

    await get_event_bus().publish_event(
        tenant_id,
        "supervision.intervention",
        {
            "intervention_id": str(interv.id),
            "supervised_agent_id": str(supervised_agent_id),
            "kind": kind,
        },
        source=f"oc8/supervision/{interv.id}",
    )
    return interv


async def supervise_checkpoint(
    db: AsyncSession,
    *,
    checkpoint: AgentCheckpoint,
    assignment: SupervisionAssignment,
    policy: SupervisionPolicy,
    effective_score: float | None = None,
    allowed_override: list[str] | None = None,
) -> SupervisionIntervention | None:
    """Given a scored checkpoint, choose and apply an intervention per the policy.
    pause_escalate additionally pauses the supervised agent (reaches a human).

    `effective_score` lets a caller substitute a blended score (e.g. the tier-1
    score floored by a tier-2 judge verdict); `allowed_override` lets it narrow
    the rungs that may fire (the loop hook restricts automatic application to
    steer/pause_escalate). Both default to today's policy-driven behaviour."""
    score = (
        effective_score
        if effective_score is not None
        else (float(checkpoint.drift_score) if checkpoint.drift_score is not None else None)
    )
    allowed = (
        allowed_override
        if allowed_override is not None
        else [str(x) for x in policy.allowed_interventions]
    )
    kind = choose_intervention(score, policy.drift_thresholds, allowed)
    if kind is None:
        return None
    outcome = "resumed"
    if kind == "pause_escalate":
        agent = await db.get(m.Agent, assignment.supervised_agent_id)
        if agent is not None:
            agent.status = "paused"
            agent.pause_reason = "supervision"
        outcome = "escalated"
    reason: dict[str, Any] = {"score": score, "verdict": checkpoint.verdict}
    if checkpoint.judge_verdict:
        reason["judge"] = {
            "verdict": checkpoint.judge_verdict,
            **(checkpoint.judge_evidence or {}),
        }
    return await record_intervention(
        db,
        tenant_id=checkpoint.tenant_id,
        supervisor_agent_id=assignment.supervisor_agent_id,
        supervised_agent_id=assignment.supervised_agent_id,
        task_id=checkpoint.task_id,
        kind=kind,
        reason=reason,
        checkpoint_id=checkpoint.id,
        outcome=outcome,
    )
