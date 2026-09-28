"""Checkpoint emission + tier-1 drift evaluation (§8.6). A checkpoint records a
structured progress summary and (when an anchor exists) a drift score + verdict."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.hooks.bus import dispatch_action
from oc8.hooks.executor import InProcessExecutor
from oc8.hooks.types import HookCtx
from oc8.models.supervision import AgentCheckpoint, TaskAnchor
from oc8.supervision.detection import compute_drift_score, verdict_from_score


async def _next_seq(db: AsyncSession, agent_id: uuid.UUID, task_id: uuid.UUID) -> int:
    current = (
        await db.execute(
            select(func.max(AgentCheckpoint.seq)).where(
                AgentCheckpoint.agent_id == agent_id, AgentCheckpoint.task_id == task_id
            )
        )
    ).scalar_one_or_none()
    return (current or 0) + 1


async def record_checkpoint(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    state_summary: str,
    drift_score: float | None = None,
    verdict: str | None = None,
    context_ref: str | None = None,
) -> AgentCheckpoint:
    cp = AgentCheckpoint(
        tenant_id=tenant_id,
        agent_id=agent_id,
        task_id=task_id,
        seq=await _next_seq(db, agent_id, task_id),
        state_summary=state_summary,
        drift_score=drift_score,
        verdict=verdict,
        context_ref=context_ref,
    )
    db.add(cp)
    await db.flush()
    await dispatch_action(
        HookCtx(tenant_id=tenant_id),
        "supervision.checkpoint.recorded",
        executor_default=InProcessExecutor(),
        checkpoint_id=str(cp.id),
        agent_id=str(agent_id),
    )
    return cp


async def evaluate_and_checkpoint(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    state_summary: str,
    anchor: TaskAnchor | None,
    thresholds: dict[str, Any],
    trajectory_embedding: list[float] | None = None,
    uncited_ratio: float = 0.0,
    constraint_violations: int = 0,
    context_ref: str | None = None,
) -> AgentCheckpoint:
    """Compute a tier-1 drift score against the task anchor, derive a verdict, and
    persist a checkpoint. With no anchor, score/verdict stay None (no baseline)."""
    score: float | None = None
    verdict: str | None = None
    if anchor is not None:
        anchor_embedding = (
            anchor.anchor_embedding if isinstance(anchor.anchor_embedding, list) else None
        )
        score = compute_drift_score(
            anchor_embedding=anchor_embedding,
            trajectory_embedding=trajectory_embedding,
            uncited_ratio=uncited_ratio,
            constraint_violations=constraint_violations,
        )
        verdict = verdict_from_score(score, thresholds)
    return await record_checkpoint(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        task_id=task_id,
        state_summary=state_summary,
        drift_score=score,
        verdict=verdict,
        context_ref=context_ref,
    )
