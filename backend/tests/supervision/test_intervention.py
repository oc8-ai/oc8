from __future__ import annotations

import uuid

from oc8 import models as em
from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.supervision.checkpoints import record_checkpoint
from oc8.supervision.intervention import choose_intervention, supervise_checkpoint
from oc8.supervision.service import assign_supervisor, create_policy
from tests.conftest import AppSessionFactory

_TH = {"steer": 0.35, "rewind": 0.55, "escalate": 0.75}


def test_choose_intervention_ladder() -> None:
    allowed = ["steer", "rewind", "pause_escalate"]
    assert choose_intervention(None, _TH, allowed) is None
    assert choose_intervention(0.1, _TH, allowed) is None  # on_track
    assert choose_intervention(0.4, _TH, allowed) == "steer"
    assert choose_intervention(0.6, _TH, allowed) == "rewind"
    assert choose_intervention(0.9, _TH, allowed) == "pause_escalate"
    # off_track but only steer allowed -> downgrade to steer
    assert choose_intervention(0.9, _TH, ["steer"]) == "steer"
    # nothing allowed -> None
    assert choose_intervention(0.9, _TH, []) is None


async def test_supervise_checkpoint_pauses_on_escalation(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    task = uuid.uuid4()
    async with app_session(tenant) as db:
        supervisor = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="head")
        supervised = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="worker")
        db.add_all([supervisor, supervised])
        await db.flush()
        policy = await create_policy(
            db,
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            checkpoint_every={},
            drift_thresholds=_TH,
            allowed_interventions=["steer", "rewind", "pause_escalate"],
        )
        assignment = await assign_supervisor(
            db,
            tenant_id=tenant,
            supervisor_agent_id=supervisor.id,
            supervised_agent_id=supervised.id,
            policy_id=policy.id,
        )
        cp = await record_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised.id,
            task_id=task,
            state_summary="badly off",
            drift_score=0.9,
            verdict="off_track",
        )
        interv = await supervise_checkpoint(db, checkpoint=cp, assignment=assignment, policy=policy)
        assert interv is not None and interv.kind == "pause_escalate"
        assert interv.outcome == "escalated"
        # the supervised agent is paused (reaches a human)
        reloaded = await db.get(m.Agent, supervised.id)
        assert reloaded is not None and reloaded.status == "paused"

    async with app_session(tenant) as db:
        # an on-track checkpoint yields no intervention
        cp2 = await record_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            state_summary="fine",
            drift_score=0.1,
            verdict="on_track",
        )
        policy2 = await create_policy(
            db,
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            checkpoint_every={},
            drift_thresholds=_TH,
            allowed_interventions=["steer"],
        )
        assignment2 = em.SupervisionAssignment(
            tenant_id=tenant,
            supervisor_agent_id=uuid.uuid4(),
            supervised_agent_id=uuid.uuid4(),
            policy_id=policy2.id,
        )
        assert (
            await supervise_checkpoint(db, checkpoint=cp2, assignment=assignment2, policy=policy2)
        ) is None
