from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from oc8 import models as em
from oc8.constants import ACME_TENANT_ID
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_supervision_models_persist_with_rls(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    supervisor, supervised, task = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        policy = em.SupervisionPolicy(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            checkpoint_every={"tool_calls": 5},
            drift_thresholds={"steer": 0.35, "rewind": 0.55, "escalate": 0.75},
            allowed_interventions=["steer", "rewind"],
        )
        db.add(policy)
        await db.flush()
        db.add(
            em.SupervisionAssignment(
                tenant_id=tenant,
                supervisor_agent_id=supervisor,
                supervised_agent_id=supervised,
                policy_id=policy.id,
            )
        )
        anchor = em.TaskAnchor(
            tenant_id=tenant,
            task_id=task,
            objective="Ship the feature",
            acceptance_criteria={"tests": "green"},
            constraints={"budget_eur": 1000},
        )
        db.add(anchor)
        cp = em.AgentCheckpoint(
            tenant_id=tenant,
            agent_id=supervised,
            task_id=task,
            seq=1,
            state_summary="working",
            drift_score=0.2,
            verdict="on_track",
        )
        db.add(cp)
        await db.flush()
        db.add(
            em.SupervisionIntervention(
                tenant_id=tenant,
                supervisor_agent_id=supervisor,
                supervised_agent_id=supervised,
                task_id=task,
                checkpoint_id=cp.id,
                kind="steer",
                reason={"why": "drift"},
            )
        )
        await db.flush()
        policy_id = policy.id

    async with app_session(tenant) as db:
        loaded = await db.get(em.SupervisionPolicy, policy_id)
        assert loaded is not None and loaded.allowed_interventions == ["steer", "rewind"]
        enabled = (
            await db.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname='supervision_policy'")
            )
        ).scalar_one()
    assert enabled is True
