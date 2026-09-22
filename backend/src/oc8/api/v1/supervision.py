"""Supervision endpoints: policies, assignments, task anchors, interventions."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import MANAGE, SUPERVISION, VIEW, perm
from oc8.schemas.base import CamelModel
from oc8.schemas.requests import UpdatePolicyRequest
from oc8.supervision.service import (
    RuntimeCapabilityError,
    SupervisionError,
    assign_supervisor,
    clear_supervisor,
    create_policy,
    create_task_anchor,
    get_supervisor,
    set_supervisor,
)

router = APIRouter()


class PolicyRequest(CamelModel):
    department_id: uuid.UUID
    checkpoint_every: dict[str, Any] = {}
    drift_thresholds: dict[str, Any] = {}
    allowed_interventions: list[str] = []
    judge_model_config_id: uuid.UUID | None = None
    sampling_rate: float = 1.0


class AssignmentRequest(CamelModel):
    supervisor_agent_id: uuid.UUID
    supervised_agent_id: uuid.UUID
    policy_id: uuid.UUID


class AnchorRequest(CamelModel):
    task_id: uuid.UUID
    objective: str
    acceptance_criteria: dict[str, Any] = {}
    constraints: dict[str, Any] = {}
    anchor_embedding: list[float] | None = None


class PolicyDTO(CamelModel):
    id: str
    department_id: str
    allowed_interventions: list[str]
    checkpoint_every: dict[str, Any] = {}
    drift_thresholds: dict[str, Any] = {}
    sampling_rate: float = 1.0
    judge_model_config_id: str | None = None
    judge_rubric: dict[str, Any] | None = None


class AssignmentDTO(CamelModel):
    id: str
    supervisor_agent_id: str
    supervised_agent_id: str
    policy_id: str


class AnchorDTO(CamelModel):
    id: str
    task_id: str
    objective: str


class InterventionDTO(CamelModel):
    id: str
    supervised_agent_id: str
    task_id: str
    kind: str
    outcome: str | None = None


class SupervisorRequest(CamelModel):
    supervisor_agent_id: uuid.UUID | None = None
    drift_guard: bool = True


class SupervisorDTO(CamelModel):
    supervised_agent_id: str
    supervisor_agent_id: str | None = None
    policy_id: str | None = None
    drift_guard: bool


def _policy_dto(policy: m.SupervisionPolicy) -> PolicyDTO:
    return PolicyDTO(
        id=str(policy.id),
        department_id=str(policy.department_id),
        allowed_interventions=[str(x) for x in policy.allowed_interventions],
        checkpoint_every=policy.checkpoint_every,
        drift_thresholds=policy.drift_thresholds,
        sampling_rate=float(policy.sampling_rate),
        judge_model_config_id=(
            str(policy.judge_model_config_id) if policy.judge_model_config_id else None
        ),
        judge_rubric=policy.judge_rubric,
    )


@router.post(
    "/supervision/policies",
    response_model=PolicyDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(SUPERVISION, MANAGE)))],
)
async def create_policy_endpoint(
    body: PolicyRequest, db: DbSession, principal: CurrentPrincipal
) -> PolicyDTO:
    policy = await create_policy(
        db,
        tenant_id=principal.tenant_id,
        department_id=body.department_id,
        checkpoint_every=body.checkpoint_every,
        drift_thresholds=body.drift_thresholds,
        allowed_interventions=body.allowed_interventions,
        judge_model_config_id=body.judge_model_config_id,
        sampling_rate=body.sampling_rate,
    )
    return _policy_dto(policy)


@router.get(
    "/supervision/policies",
    response_model=list[PolicyDTO],
    dependencies=[Depends(require_permission(perm(SUPERVISION, VIEW)))],
)
async def list_policies(db: DbSession, principal: CurrentPrincipal) -> list[PolicyDTO]:
    rows = (await db.execute(select(m.SupervisionPolicy))).scalars().all()
    return [_policy_dto(p) for p in rows]


@router.patch(
    "/supervision/policies/{policy_id}",
    response_model=PolicyDTO,
    dependencies=[Depends(require_permission(perm(SUPERVISION, MANAGE)))],
)
async def update_policy(
    policy_id: uuid.UUID,
    body: UpdatePolicyRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> PolicyDTO:
    policy = await db.get(m.SupervisionPolicy, policy_id)
    if policy is None or policy.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "policy not found")
    data = body.model_dump(exclude_unset=True)
    for field in (
        "checkpoint_every",
        "drift_thresholds",
        "allowed_interventions",
        "judge_model_config_id",
        "sampling_rate",
        "judge_rubric",
    ):
        if field in data:
            setattr(policy, field, data[field])
    await db.flush()
    return _policy_dto(policy)


@router.post(
    "/supervision/assignments",
    response_model=AssignmentDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(SUPERVISION, MANAGE)))],
)
async def create_assignment(
    body: AssignmentRequest, db: DbSession, principal: CurrentPrincipal
) -> AssignmentDTO:
    try:
        assignment = await assign_supervisor(
            db,
            tenant_id=principal.tenant_id,
            supervisor_agent_id=body.supervisor_agent_id,
            supervised_agent_id=body.supervised_agent_id,
            policy_id=body.policy_id,
        )
    except SupervisionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return AssignmentDTO(
        id=str(assignment.id),
        supervisor_agent_id=str(assignment.supervisor_agent_id),
        supervised_agent_id=str(assignment.supervised_agent_id),
        policy_id=str(assignment.policy_id),
    )


@router.post(
    "/task-anchors",
    response_model=AnchorDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(SUPERVISION, MANAGE)))],
)
async def create_anchor(
    body: AnchorRequest, db: DbSession, principal: CurrentPrincipal
) -> AnchorDTO:
    anchor = await create_task_anchor(
        db,
        tenant_id=principal.tenant_id,
        task_id=body.task_id,
        objective=body.objective,
        acceptance_criteria=body.acceptance_criteria,
        constraints=body.constraints,
        anchor_embedding=body.anchor_embedding,
    )
    return AnchorDTO(id=str(anchor.id), task_id=str(anchor.task_id), objective=anchor.objective)


@router.get(
    "/supervision/interventions",
    response_model=list[InterventionDTO],
    dependencies=[Depends(require_permission(perm(SUPERVISION, VIEW)))],
)
async def list_interventions(db: DbSession, principal: CurrentPrincipal) -> list[InterventionDTO]:
    rows = (await db.execute(select(m.SupervisionIntervention))).scalars().all()
    return [
        InterventionDTO(
            id=str(i.id),
            supervised_agent_id=str(i.supervised_agent_id),
            task_id=str(i.task_id),
            kind=i.kind,
            outcome=i.outcome,
        )
        for i in rows
    ]


@router.get(
    "/agents/{agent_id}/supervisor",
    response_model=SupervisorDTO,
    dependencies=[Depends(require_permission(perm(SUPERVISION, VIEW)))],
)
async def read_supervisor(
    agent_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> SupervisorDTO:
    assignment = await get_supervisor(db, supervised_agent_id=agent_id)
    if assignment is None:
        return SupervisorDTO(supervised_agent_id=str(agent_id), drift_guard=False)
    return SupervisorDTO(
        supervised_agent_id=str(agent_id),
        supervisor_agent_id=str(assignment.supervisor_agent_id),
        policy_id=str(assignment.policy_id),
        drift_guard=True,
    )


@router.put(
    "/agents/{agent_id}/supervisor",
    response_model=SupervisorDTO,
    dependencies=[Depends(require_permission(perm(SUPERVISION, MANAGE)))],
)
async def update_supervisor(
    agent_id: uuid.UUID, body: SupervisorRequest, db: DbSession, principal: CurrentPrincipal
) -> SupervisorDTO:
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if not body.drift_guard or body.supervisor_agent_id is None:
        await clear_supervisor(db, supervised_agent_id=agent_id)
        return SupervisorDTO(supervised_agent_id=str(agent_id), drift_guard=False)
    try:
        assignment = await set_supervisor(
            db,
            tenant_id=principal.tenant_id,
            supervised_agent_id=agent_id,
            supervisor_agent_id=body.supervisor_agent_id,
            department_id=agent.department_id,
        )
    except RuntimeCapabilityError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return SupervisorDTO(
        supervised_agent_id=str(agent_id),
        supervisor_agent_id=str(assignment.supervisor_agent_id),
        policy_id=str(assignment.policy_id),
        drift_guard=True,
    )
