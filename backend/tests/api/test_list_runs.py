"""GET /runs (run step timeline, Task 10).

Every assertion here counts only rows this test created: ACME_TENANT_ID is
shared across the suite with no per-test rollback, so each test mints its own
tenant and never asserts a tenant-wide total.
"""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _runs(tenant: uuid.UUID, query: str = "") -> tuple[int, object]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/api/v1/runs{query}", headers=_h(tenant))
            return resp.status_code, (resp.json() if resp.content else None)


async def _seed(db: object, tenant: uuid.UUID, states: list[str]) -> list[uuid.UUID]:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Nora")
    db.add(agent)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    ids: list[uuid.UUID] = []
    for state in states:
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state=state,
            context={
                "steps": 1,
                "toolCalls": [
                    {
                        "tool": "search_records",
                        "arguments": {},
                        "step": 1,
                        "connection": "odoo",
                        "state": "done",
                    }
                ],
                "stepTimings": [{"step": 1, "step_wall_ms": 1200}],
            },
        )
        db.add(run)  # type: ignore[attr-defined]
        await db.flush()  # type: ignore[attr-defined]
        ids.append(run.id)
    return ids


async def test_it_returns_this_tenants_runs_with_their_steps(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        created = await _seed(db, tenant, ["running", "done"])
        await db.commit()

    code, body = await _runs(tenant)
    assert code == 200, body
    assert isinstance(body, list)
    returned = {row["id"] for row in body}
    assert {str(i) for i in created} <= returned
    row = next(r for r in body if r["id"] == str(created[0]))
    assert row["toolCalls"][0]["state"] == "done"
    assert row["stepTimings"][0]["stepWallMs"] == 1200


async def test_the_state_filter_narrows_to_the_named_states(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        parked, running, _done = await _seed(
            db, tenant, ["waiting_for_approval", "running", "done"]
        )
        await db.commit()

    code, body = await _runs(tenant, "?state=waiting_for_approval,waiting_for_input")
    assert code == 200, body
    assert isinstance(body, list)
    ids = {row["id"] for row in body}
    assert str(parked) in ids
    assert str(running) not in ids


async def test_an_unknown_state_is_a_400_not_an_empty_list(
    app_session: AppSessionFactory,
) -> None:
    """An empty list reads as "nothing is parked", which is the one wrong
    answer a typo must not be able to produce."""
    tenant = uuid.uuid4()
    code, body = await _runs(tenant, "?state=waiting_for_aproval")
    assert code == 400, body


async def test_the_limit_is_clamped(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _seed(db, tenant, ["done"] * 5)
        await db.commit()

    code, body = await _runs(tenant, "?limit=2")
    assert code == 200, body
    assert isinstance(body, list) and len(body) == 2

    code, body = await _runs(tenant, "?limit=9999")
    assert code == 200, body
    assert isinstance(body, list) and len(body) <= 20


async def test_another_tenants_runs_are_not_returned(
    app_session: AppSessionFactory,
) -> None:
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    async with app_session(theirs) as db:
        hidden = await _seed(db, theirs, ["running"])
        await db.commit()
    async with app_session(mine) as db:
        await _seed(db, mine, ["running"])
        await db.commit()

    code, body = await _runs(mine)
    assert code == 200, body
    assert isinstance(body, list)
    assert str(hidden[0]) not in {row["id"] for row in body}
