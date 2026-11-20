"""Thin B6/B7 wrappers over the shared record-claim implementation."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from oc8 import models as m
from oc8.agent.harness.stages.b_blast_radius import check_blast_radius
from oc8.agent.harness.stages.b_claims import claim_write
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _actors(
    db: object, tenant: uuid.UUID
) -> tuple[m.Agent, m.AgentRun, m.Agent, m.AgentRun]:
    dept = m.Department(tenant_id=tenant, name="Operations", frame={})
    db.add(dept)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    first = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Alex",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    second = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Sam",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add_all([first, second])  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    first_run = m.AgentRun(tenant_id=tenant, agent_id=first.id, state="running", context={})
    second_run = m.AgentRun(tenant_id=tenant, agent_id=second.id, state="running", context={})
    db.add_all([first_run, second_run])  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return first, first_run, second, second_run


async def test_claim_wrapper_returns_the_existing_holders_refusal(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first, first_run, second, second_run = await _actors(db, tenant)
        identity = ("case", "42")
        assert (
            await claim_write(
                db,
                tenant_id=tenant,
                run_id=first_run.id,
                agent_id=first.id,
                identity=identity,
                label="Case 42",
            )
            is None
        )
        denied = await claim_write(
            db,
            tenant_id=tenant,
            run_id=second_run.id,
            agent_id=second.id,
            identity=identity,
            label="Case 42",
        )

    assert denied is not None
    assert denied.startswith("ERROR:")
    assert "Alex" in denied
    assert "next item" in denied


async def test_blast_wrapper_refuses_a_second_distinct_record(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run, _second, _second_run = await _actors(db, tenant)
        assert (
            await claim_write(
                db,
                tenant_id=tenant,
                run_id=run.id,
                agent_id=agent.id,
                identity=("case", "1"),
                label="Case 1",
            )
            is None
        )
        denied = await check_blast_radius(
            db,
            tenant_id=tenant,
            run_id=run.id,
            frame={"limits": {"records_per_run": 1}},
            identity=("case", "2"),
        )
        same_record = await check_blast_radius(
            db,
            tenant_id=tenant,
            run_id=run.id,
            frame={"limits": {"records_per_run": 1}},
            identity=("case", "1"),
        )

    assert denied is not None and denied.startswith("ERROR:")
    assert "limit of 1" in denied
    assert same_record is None


async def test_wrappers_skip_database_work_without_a_run() -> None:
    db = AsyncMock()
    identity = ("case", "42")

    assert (
        await check_blast_radius(
            db,
            tenant_id=uuid.uuid4(),
            run_id=None,
            frame={"limits": {"records_per_run": 1}},
            identity=identity,
        )
        is None
    )
    assert (
        await claim_write(
            db,
            tenant_id=uuid.uuid4(),
            run_id=None,
            agent_id=uuid.uuid4(),
            identity=identity,
            label="Case 42",
        )
        is None
    )
    db.execute.assert_not_awaited()
