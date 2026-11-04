"""GET /agents/{id}/runs (the agent page's History tab).

Each test mints its own tenant, so nothing here depends on rows another test
left behind.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _get(tenant: uuid.UUID, path: str) -> tuple[int, object]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/api/v1{path}", headers=_h(tenant))
            return resp.status_code, (resp.json() if resp.content else None)


async def _agent(db: object, tenant: uuid.UUID) -> m.Agent:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Nora")
    db.add(agent)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return agent


async def _run(
    db: object,
    agent: m.Agent,
    *,
    state: str,
    created: dt.datetime,
    context: dict[str, object] | None = None,
    source: str = "manual",
    transitions: list[tuple[str, dt.datetime]] = (),  # type: ignore[assignment]
) -> uuid.UUID:
    run = m.AgentRun(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        state=state,
        source=source,
        context=context or {},
        created_at=created,
    )
    db.add(run)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    for to_state, at in transitions:
        db.add(  # type: ignore[attr-defined]
            m.RunStateTransition(tenant_id=agent.tenant_id, run_id=run.id, to_state=to_state, at=at)
        )
    await db.flush()  # type: ignore[attr-defined]
    return run.id


async def test_it_lists_one_agents_runs_newest_first_with_duration_and_reason(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        other = await _agent(db, tenant)
        done = await _run(
            db,
            agent,
            state="done",
            created=T0,
            source="cron",
            context={"task": "Summarise   the\ninbox", "output": "All quiet.", "steps": 2},
            transitions=[
                ("queued", T0),
                ("running", T0 + dt.timedelta(seconds=5)),
                ("done", T0 + dt.timedelta(seconds=17)),
            ],
        )
        failed = await _run(
            db,
            agent,
            state="failed",
            created=T0 + dt.timedelta(minutes=5),
            context={"task": "x", "output": "Model exceeded its token budget"},
            transitions=[("running", T0 + dt.timedelta(minutes=5, seconds=1))],
        )
        await _run(db, other, state="done", created=T0)
        await db.commit()
        agent_id = agent.id

    code, body = await _get(tenant, f"/agents/{agent_id}/runs")
    assert code == 200, body
    assert isinstance(body, dict)
    assert body["totalCount"] == 2
    items = body["items"]
    assert [i["id"] for i in items] == [str(failed), str(done)]

    first, second = items
    assert first["state"] == "failed"
    assert first["failureReason"] == "Model exceeded its token budget"
    # The failure reason is the answer of a failed run -- not repeated as output.
    assert first["outputExcerpt"] is None
    # No terminal transition was recorded, so there is no end to measure to.
    assert first["durationMs"] is None

    assert second["source"] == "cron"
    assert second["taskExcerpt"] == "Summarise the inbox"
    assert second["outputExcerpt"] == "All quiet."
    assert second["steps"] == 2
    # Measured from the first `running`, not from when it was queued.
    assert second["durationMs"] == 12_000
    assert second["failureReason"] is None


async def test_it_pages_with_limit_and_offset(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        ids = [
            await _run(db, agent, state="done", created=T0 + dt.timedelta(minutes=i))
            for i in range(5)
        ]
        await db.commit()
        agent_id = agent.id

    newest_first = [str(i) for i in reversed(ids)]
    code, body = await _get(tenant, f"/agents/{agent_id}/runs?limit=2&offset=0")
    assert code == 200, body
    assert body["totalCount"] == 5  # type: ignore[index]
    assert [i["id"] for i in body["items"]] == newest_first[:2]  # type: ignore[index]

    code, body = await _get(tenant, f"/agents/{agent_id}/runs?limit=2&offset=4")
    assert code == 200, body
    assert [i["id"] for i in body["items"]] == newest_first[4:]  # type: ignore[index]

    code, body = await _get(tenant, f"/agents/{agent_id}/runs?limit=9999")
    assert code == 422, body


async def test_an_agent_without_runs_gets_an_empty_page(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        await db.commit()
        agent_id = agent.id

    code, body = await _get(tenant, f"/agents/{agent_id}/runs")
    assert code == 200, body
    assert body == {"items": [], "totalCount": 0}


async def test_another_tenants_agent_is_a_404(app_session: AppSessionFactory) -> None:
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    async with app_session(theirs) as db:
        agent = await _agent(db, theirs)
        await _run(db, agent, state="done", created=T0)
        await db.commit()
        agent_id = agent.id

    code, body = await _get(mine, f"/agents/{agent_id}/runs")
    assert code == 404, body
