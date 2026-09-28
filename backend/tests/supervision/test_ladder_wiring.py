from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as em
from oc8 import models as m
from oc8.supervision.intervention import supervise_checkpoint
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _fixture(
    s: Any,
    tenant: uuid.UUID,
    *,
    allowed: list[str],
    drift_score: float,
    judge_verdict: str | None = None,
    judge_evidence: dict[str, Any] | None = None,
) -> tuple[em.AgentCheckpoint, em.SupervisionAssignment, em.SupervisionPolicy]:
    sup, sub = uuid.uuid4(), uuid.uuid4()
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    s.add(dept)
    await s.flush()
    s.add(m.Agent(id=sub, tenant_id=tenant, department_id=dept.id, name="sub", status="running"))
    policy = em.SupervisionPolicy(
        tenant_id=tenant,
        department_id=dept.id,
        checkpoint_every={},
        drift_thresholds={"steer": 0.35, "rewind": 0.55, "escalate": 0.75},
        allowed_interventions=allowed,
        sampling_rate=1.0,
    )
    s.add(policy)
    await s.flush()
    assignment = em.SupervisionAssignment(
        tenant_id=tenant,
        supervisor_agent_id=sup,
        supervised_agent_id=sub,
        policy_id=policy.id,
    )
    cp = em.AgentCheckpoint(
        tenant_id=tenant,
        agent_id=sub,
        task_id=uuid.uuid4(),
        seq=1,
        state_summary="s",
        drift_score=drift_score,
        verdict="on_track",
        judge_verdict=judge_verdict,
        judge_evidence=judge_evidence,
    )
    s.add_all([assignment, cp])
    await s.flush()
    return cp, assignment, policy


async def test_judge_offtrack_escalates_despite_low_tier1_score(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp, assignment, policy = await _fixture(
            s,
            tenant,
            allowed=["steer", "pause_escalate"],
            drift_score=0.05,
            judge_verdict="off_track",
            judge_evidence={"evidence": ["looped"]},
        )
        iv = await supervise_checkpoint(
            s,
            checkpoint=cp,
            assignment=assignment,
            policy=policy,
            effective_score=0.75,  # VERDICT_FLOOR["off_track"]
            allowed_override=["steer", "pause_escalate"],
        )
        assert iv is not None and iv.kind == "pause_escalate"
        assert iv.reason["judge"]["verdict"] == "off_track"
        assert iv.reason["judge"]["evidence"] == ["looped"]


async def test_rewind_is_never_auto_applied(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp, assignment, policy = await _fixture(
            s,
            tenant,
            allowed=["steer", "rewind", "pause_escalate"],
            drift_score=0.6,
        )
        iv = await supervise_checkpoint(
            s,
            checkpoint=cp,
            assignment=assignment,
            policy=policy,
            effective_score=0.6,
            allowed_override=["steer", "pause_escalate"],  # rewind filtered out
        )
        assert iv is not None and iv.kind == "steer"  # downgraded, never rewind


async def test_empty_allowed_fires_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp, assignment, policy = await _fixture(s, tenant, allowed=["rewind"], drift_score=0.9)
        iv = await supervise_checkpoint(
            s,
            checkpoint=cp,
            assignment=assignment,
            policy=policy,
            effective_score=0.9,
            allowed_override=[],
        )
        assert iv is None


async def test_defaults_preserve_policy_driven_behaviour(
    app_session: AppSessionFactory,
) -> None:
    """No overrides -> the checkpoint's own drift_score and the policy's own
    allowed_interventions decide, exactly as before this task."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp, assignment, policy = await _fixture(
            s,
            tenant,
            allowed=["steer", "rewind", "pause_escalate"],
            drift_score=0.6,
        )
        iv = await supervise_checkpoint(s, checkpoint=cp, assignment=assignment, policy=policy)
        assert iv is not None and iv.kind == "rewind"
        assert iv.reason == {"score": 0.6, "verdict": "on_track"}
