from __future__ import annotations

import uuid

import pytest

from oc8.constants import ACME_TENANT_ID
from oc8.supervision.checkpoints import evaluate_and_checkpoint, record_checkpoint
from oc8.supervision.service import create_task_anchor
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_TH = {"steer": 0.35, "rewind": 0.55, "escalate": 0.75}


async def test_checkpoint_seq_autoincrements(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent, task = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        c1 = await record_checkpoint(
            db, tenant_id=tenant, agent_id=agent, task_id=task, state_summary="s1"
        )
        c2 = await record_checkpoint(
            db, tenant_id=tenant, agent_id=agent, task_id=task, state_summary="s2"
        )
        assert c1.seq == 1 and c2.seq == 2


async def test_evaluate_scores_and_verdicts(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent, task = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        anchor = await create_task_anchor(
            db,
            tenant_id=tenant,
            task_id=task,
            objective="Ship it",
            acceptance_criteria={},
            constraints={},
            anchor_embedding=[1.0, 0.0],
        )
        cp = await evaluate_and_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=agent,
            task_id=task,
            state_summary="drifted",
            anchor=anchor,
            thresholds=_TH,
            trajectory_embedding=[0.0, 1.0],  # orthogonal -> off_track
        )
        assert cp.verdict == "off_track"
        assert cp.drift_score is not None and float(cp.drift_score) >= 0.55

    # no anchor -> no score/verdict baseline
    async with app_session(tenant) as db:
        cp2 = await evaluate_and_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=agent,
            task_id=uuid.uuid4(),
            state_summary="no anchor",
            anchor=None,
            thresholds=_TH,
        )
        assert cp2.verdict is None and cp2.drift_score is None
