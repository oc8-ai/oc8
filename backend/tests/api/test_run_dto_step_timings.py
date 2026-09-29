"""`stepTimings` reaches the frontend (run step timeline, Task 6).

Both run-reading routes go through `run_to_dto`, so both are asserted: the
chat one is what the timeline in the copilot dock actually calls.

`context["stepTimings"]` is stored SNAKE_CASE by
`oc8.agent.harness.step_timing` (`step`, `model_wait_ms`, `ttft_ms`,
`tool_wait_ms`, `step_wall_ms`) -- that shape must never change, since the
eval CLI (`backend/evals/oc8_evals/*`) and that module's own
`latency_lines()` read it directly. `run_to_dto` translates each entry to
camelCase (`oc8.runtime.step_record.step_timing_dto`) before it reaches the
wire, so every fixture below stores the real snake_case shape and asserts
against the translated camelCase response.
"""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# The real stored shape -- step_timing.py's own "public five-key dict".
_STORED = [
    {"step": 1, "model_wait_ms": 2383, "ttft_ms": 412, "tool_wait_ms": 5, "step_wall_ms": 3628},
    {"step": 2, "model_wait_ms": 900, "ttft_ms": None, "tool_wait_ms": 0, "step_wall_ms": 950},
]
# What run_to_dto must translate that into on the wire.
_WIRE = [
    {"step": 1, "modelWaitMs": 2383, "ttftMs": 412, "toolWaitMs": 5, "stepWallMs": 3628},
    {"step": 2, "modelWaitMs": 900, "ttftMs": None, "toolWaitMs": 0, "stepWallMs": 950},
]


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _agent_id(db: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
    db.add(department)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Nora")
    db.add(agent)
    await db.flush()
    return agent.id


async def test_get_run_returns_step_timings(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id = await _agent_id(db, tenant)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent_id,
            state="done",
            context={"steps": 2, "toolCalls": [], "stepTimings": _STORED},
        )
        db.add(run)
        await db.flush()
        run_id = run.id
        await db.commit()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/api/v1/runs/{run_id}", headers=_h(tenant))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stepTimings"] == _WIRE
    assert body["stepTimings"][1]["ttftMs"] is None


async def test_a_run_with_no_timings_returns_an_empty_list_not_null(
    app_session: AppSessionFactory,
) -> None:
    """`undefined` and `[]` are different code paths in the view model, and a
    `null` here would put a third one in front of it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id = await _agent_id(db, tenant)
        run = m.AgentRun(tenant_id=tenant, agent_id=agent_id, state="done", context={})
        db.add(run)
        await db.flush()
        run_id = run.id
        await db.commit()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/api/v1/runs/{run_id}", headers=_h(tenant))

    assert resp.status_code == 200, resp.text
    assert resp.json()["stepTimings"] == []


async def test_step_timings_snake_case_storage_is_translated_to_camel_case_on_the_wire(
    app_session: AppSessionFactory,
) -> None:
    """Task 6 addendum: the stored JSONB keeps the snake_case keys
    `oc8.agent.harness.step_timing` writes. This is the test that actually
    proves `run_to_dto` performs the translation rather than a verbatim
    pass-through -- a fixture that stores already-camelCase entries (as
    above) would pass even with a naive pass-through and would never catch a
    regression here.
    """
    tenant = uuid.uuid4()
    raw = [
        {"step": 1, "model_wait_ms": 2383, "ttft_ms": 412, "tool_wait_ms": 5, "step_wall_ms": 3628}
    ]
    async with app_session(tenant) as db:
        agent_id = await _agent_id(db, tenant)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent_id,
            state="done",
            context={"steps": 1, "toolCalls": [], "stepTimings": raw},
        )
        db.add(run)
        await db.flush()
        run_id = run.id
        await db.commit()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/api/v1/runs/{run_id}", headers=_h(tenant))

    assert resp.status_code == 200, resp.text
    entry = resp.json()["stepTimings"][0]
    assert entry["modelWaitMs"] == 2383
    assert "model_wait_ms" not in entry
