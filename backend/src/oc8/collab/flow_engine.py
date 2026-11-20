"""Thin flow engine: start a run, fire stage handoffs, advance on completion.
It creates handoffs and tracks state — all work happens in the target
departments through their own agents/permissions (invariant I3, §14a.4)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.collab.contracts import apply_payload_map
from oc8.collab.flow_spec import FlowSpec, Stage, parse_flow_spec
from oc8.collab.handoff import create_handoff, reject_handoff

if TYPE_CHECKING:
    from oc8.auth.principal import Principal

# Flow-created handoffs are attributed to a stable non-agent actor.
_FLOW_ACTOR = uuid.UUID("00000000-0000-7000-8000-0000000f0000")

_OPS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


class FlowError(ValueError):
    """A flow engine operation failed."""


def _extract(path: str, context: dict[str, Any]) -> Any:
    cleaned = path[2:] if path.startswith("$.") else path.lstrip("$.")
    value: Any = context
    for part in cleaned.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def condition_ok(condition: str | None, context: dict[str, Any]) -> bool:
    if not condition:
        return True
    parts = condition.split(maxsplit=2)
    if len(parts) != 3 or parts[1] not in _OPS:
        raise FlowError(f"unsupported condition: {condition!r}")
    left = _extract(parts[0], context)
    raw = parts[2]
    try:
        right: Any = float(raw)
        left = float(left) if left is not None else None
    except ValueError:
        right = raw.strip("'\"")
    if left is None:
        return False
    return bool(_OPS[parts[1]](left, right))


async def _fire_stage(
    db: AsyncSession, *, tenant_id: uuid.UUID, run: m.FlowRun, spec: FlowSpec, stage: Stage
) -> None:
    ht = (
        await db.execute(select(m.HandoffType).where(m.HandoffType.name == stage.handoff.type))
    ).scalar_one_or_none()
    if ht is None:
        raise FlowError(f"unknown handoff type: {stage.handoff.type}")
    mapped = apply_payload_map(stage.handoff.payload_map, run.context)
    handoff = await create_handoff(
        db,
        tenant_id=tenant_id,
        handoff_type=ht,
        source_department_id=spec.trigger.from_department_id,
        target_department_id=stage.handoff.to_department_id,
        payload=mapped,
        created_by=_FLOW_ACTOR,
        gate=stage.handoff.gate,
        flow_run_id=run.id,
    )
    run.current_stages = [*run.current_stages, stage.id]
    # Remember which stage a handoff belongs to so its completion advances the
    # flow. Kept under a reserved key so it never collides with mapped payload.
    mapping = dict(run.context.get("__handoff_stage", {}))
    mapping[str(handoff.id)] = stage.id
    run.context = {**run.context, "__handoff_stage": mapping}


async def _maybe_complete(db: AsyncSession, run: m.FlowRun) -> None:
    completed = not run.current_stages
    if completed:
        run.status = "completed"
    await db.flush()
    if completed:
        from oc8.realtime.bus import get_event_bus

        await get_event_bus().publish_event(
            run.tenant_id,
            "flow_run.status",
            {"flow_run_id": str(run.id), "status": run.status},
            source=f"oc8/flow_run/{run.id}",
        )


async def start_flow_run(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    flow_version: m.FlowVersion,
    spec: FlowSpec,
    context: dict[str, Any],
) -> m.FlowRun:
    run = m.FlowRun(
        tenant_id=tenant_id,
        flow_version_id=flow_version.id,
        status="running",
        context=context,
        trigger_event=spec.trigger.event,
        current_stages=[],
    )
    db.add(run)
    await db.flush()
    for stage in spec.stages:
        if stage.after is None and condition_ok(stage.condition, context):
            await _fire_stage(db, tenant_id=tenant_id, run=run, spec=spec, stage=stage)
    await _maybe_complete(db, run)
    return run


async def advance_flow_run(
    db: AsyncSession, *, run: m.FlowRun, spec: FlowSpec, completed_stage_id: str
) -> m.FlowRun:
    run.current_stages = [s for s in run.current_stages if s != completed_stage_id]
    trigger_key = f"{completed_stage_id}.completed"
    for stage in spec.stages:
        if stage.after == trigger_key and condition_ok(stage.condition, run.context):
            await _fire_stage(db, tenant_id=run.tenant_id, run=run, spec=spec, stage=stage)
    await _maybe_complete(db, run)
    return run


async def on_handoff_completed(db: AsyncSession, handoff: m.Handoff) -> None:
    """If a completed handoff belongs to a flow run, advance that run's stage."""
    if handoff.flow_run_id is None:
        return
    # Locked so a cancel running at the same moment either sees the advanced
    # run or makes this one see `cancelled`, never both acting on a stale read.
    run = await db.get(m.FlowRun, handoff.flow_run_id, with_for_update=True)
    if run is None or run.status != "running":
        return
    stage_id = run.context.get("__handoff_stage", {}).get(str(handoff.id))
    if stage_id is None:
        return
    version = await db.get(m.FlowVersion, run.flow_version_id)
    if version is None:
        return
    spec = parse_flow_spec(version.spec)
    await advance_flow_run(db, run=run, spec=spec, completed_stage_id=stage_id)


#: The flow run statuses a cancel may still stop.
CANCELLABLE: frozenset[str] = frozenset({"running", "suspended"})


async def cancel_flow_run(
    db: AsyncSession, run: m.FlowRun, *, principal: Principal | None = None
) -> m.FlowRun:
    """Stop a flow run and the steps it still has open.

    Every handoff of the run that has not finished is rejected (the same
    transition a person's reject makes, so its status events fire), and an
    agent run working an accepted handoff's task is cancelled through the
    operator cancel the run screen uses. Completed steps keep their history.
    A run that already ended raises `FlowError`; the caller commits.
    """
    from oc8.runtime.cancellation import record_operator_cancel
    from oc8.runtime.states import TERMINAL

    if run.status not in CANCELLABLE:
        raise FlowError(f"flow run is already {run.status}")
    handoffs = (
        (await db.execute(select(m.Handoff).where(m.Handoff.flow_run_id == run.id))).scalars().all()
    )
    terminal_runs = [s.value for s in TERMINAL]
    for handoff in handoffs:
        if handoff.status not in ("pending", "accepted", "in_progress"):
            continue
        if handoff.target_task_id is not None:
            agent_runs = (
                (
                    await db.execute(
                        select(m.AgentRun).where(
                            m.AgentRun.task_id == handoff.target_task_id,
                            m.AgentRun.state.not_in(terminal_runs),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for agent_run in agent_runs:
                await record_operator_cancel(
                    db,
                    run=agent_run,
                    principal=principal,
                    actor_id=None,
                    resource_extra={"flow_run_id": str(run.id)},
                )
        await reject_handoff(db, handoff)
    run.status = "cancelled"
    run.current_stages = []
    await db.flush()
    from oc8.realtime.bus import get_event_bus

    await get_event_bus().publish_event(
        run.tenant_id,
        "flow_run.status",
        {"flow_run_id": str(run.id), "status": run.status},
        source=f"oc8/flow_run/{run.id}",
    )
    return run
