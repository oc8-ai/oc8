from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oc8 import models as em
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_judge_columns_roundtrip(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp = em.AgentCheckpoint(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            seq=1,
            state_summary="s",
            drift_score=0.2,
            verdict="on_track",
            judge_verdict="off_track",
            judge_evidence={"evidence": ["looped"], "rationale": "no progress"},
        )
        s.add(cp)
        await s.flush()
        got = (
            await s.execute(select(em.AgentCheckpoint).where(em.AgentCheckpoint.id == cp.id))
        ).scalar_one()
        assert got.verdict == "on_track"
        assert got.judge_verdict == "off_track"
        assert got.judge_evidence is not None
        assert got.judge_evidence["evidence"] == ["looped"]


async def test_judge_verdict_check_constraint(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as s:
            s.add(
                em.AgentCheckpoint(
                    tenant_id=tenant,
                    agent_id=uuid.uuid4(),
                    task_id=uuid.uuid4(),
                    seq=1,
                    state_summary="s",
                    judge_verdict="banana",
                )
            )
            await s.flush()


async def test_policy_judge_rubric_roundtrip(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        p = em.SupervisionPolicy(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            checkpoint_every={},
            drift_thresholds={},
            allowed_interventions=[],
            sampling_rate=0.5,
            judge_rubric={"text": "be strict"},
        )
        s.add(p)
        await s.flush()
        reloaded = await s.get(em.SupervisionPolicy, p.id)
        assert reloaded is not None and reloaded.judge_rubric is not None
        assert reloaded.judge_rubric["text"] == "be strict"
