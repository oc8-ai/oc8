"""Supervision foundation: policies, supervisor assignments, task goal anchors."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.models.core import Agent
from oc8.models.supervision import SupervisionAssignment, SupervisionPolicy, TaskAnchor

# Runtime negotiation is imported lazily inside ``set_supervisor`` so importing
# the service does not load the agent engine.


class SupervisionError(ValueError):
    """A supervision operation failed."""


class RuntimeCapabilityError(ValueError):
    """The supervised agent's current runtime doesn't declare the
    capability supervision requires."""


async def create_policy(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID,
    checkpoint_every: dict[str, Any],
    drift_thresholds: dict[str, Any],
    allowed_interventions: list[str],
    judge_model_config_id: uuid.UUID | None = None,
    sampling_rate: float = 1.0,
) -> SupervisionPolicy:
    policy = SupervisionPolicy(
        tenant_id=tenant_id,
        department_id=department_id,
        checkpoint_every=checkpoint_every,
        drift_thresholds=drift_thresholds,
        allowed_interventions=list(allowed_interventions),
        judge_model_config_id=judge_model_config_id,
        sampling_rate=sampling_rate,
    )
    db.add(policy)
    await db.flush()
    return policy


async def assign_supervisor(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    supervisor_agent_id: uuid.UUID,
    supervised_agent_id: uuid.UUID,
    policy_id: uuid.UUID,
) -> SupervisionAssignment:
    existing = (
        await db.execute(
            select(SupervisionAssignment).where(
                SupervisionAssignment.supervised_agent_id == supervised_agent_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise SupervisionError("agent already has a supervisor")
    assignment = SupervisionAssignment(
        tenant_id=tenant_id,
        supervisor_agent_id=supervisor_agent_id,
        supervised_agent_id=supervised_agent_id,
        policy_id=policy_id,
    )
    db.add(assignment)
    await db.flush()
    return assignment


async def create_task_anchor(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    objective: str,
    acceptance_criteria: dict[str, Any],
    constraints: dict[str, Any],
    anchor_embedding: list[float] | None = None,
) -> TaskAnchor:
    anchor = TaskAnchor(
        tenant_id=tenant_id,
        task_id=task_id,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        anchor_embedding=anchor_embedding,
    )
    db.add(anchor)
    await db.flush()
    return anchor


_DEFAULT_THRESHOLDS = {"steer": 0.35, "rewind": 0.55, "escalate": 0.75}
_DEFAULT_INTERVENTIONS = ["steer", "rewind", "pause_escalate"]


async def ensure_default_policy(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> SupervisionPolicy:
    """Reuse the department's supervision policy, or create a sane default one."""
    existing = (
        (
            await db.execute(
                select(SupervisionPolicy).where(SupervisionPolicy.department_id == department_id)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    return await create_policy(
        db,
        tenant_id=tenant_id,
        department_id=department_id,
        checkpoint_every={"tool_calls": 5},
        drift_thresholds=dict(_DEFAULT_THRESHOLDS),
        allowed_interventions=list(_DEFAULT_INTERVENTIONS),
    )


async def get_supervisor(
    db: AsyncSession, *, supervised_agent_id: uuid.UUID
) -> SupervisionAssignment | None:
    return (
        await db.execute(
            select(SupervisionAssignment).where(
                SupervisionAssignment.supervised_agent_id == supervised_agent_id
            )
        )
    ).scalar_one_or_none()


async def set_supervisor(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    supervised_agent_id: uuid.UUID,
    supervisor_agent_id: uuid.UUID,
    department_id: uuid.UUID,
) -> SupervisionAssignment:
    """Upsert the supervisor for an agent (unique per supervised agent), attaching
    the department's default supervision policy."""
    from oc8.runtime.registry import check_runtime_capabilities, resolve_runtime_plugin

    supervised_agent = await db.get(Agent, supervised_agent_id)
    if supervised_agent is not None:
        resolved = await resolve_runtime_plugin(db, tenant_id=tenant_id, agent=supervised_agent)
        if resolved is not None:
            _, version = resolved
            violations = check_runtime_capabilities(
                has_supervision=True,
                has_enabled_skills=False,
                runtime_capabilities=list(version.capabilities),
            )
            if violations:
                raise RuntimeCapabilityError(
                    f"runtime does not support supervision: {[v.reason for v in violations]}"
                )
    policy = await ensure_default_policy(db, tenant_id=tenant_id, department_id=department_id)
    existing = await get_supervisor(db, supervised_agent_id=supervised_agent_id)
    if existing is not None:
        existing.supervisor_agent_id = supervisor_agent_id
        existing.policy_id = policy.id
        await db.flush()
        return existing
    assignment = SupervisionAssignment(
        tenant_id=tenant_id,
        supervisor_agent_id=supervisor_agent_id,
        supervised_agent_id=supervised_agent_id,
        policy_id=policy.id,
    )
    db.add(assignment)
    await db.flush()
    return assignment


async def clear_supervisor(db: AsyncSession, *, supervised_agent_id: uuid.UUID) -> None:
    existing = await get_supervisor(db, supervised_agent_id=supervised_agent_id)
    if existing is not None:
        await db.delete(existing)
        await db.flush()
