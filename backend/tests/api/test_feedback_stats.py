"""`GET /stats/feedback`: thumbs up and down per agent and model, counted from
the `chat.feedback` audit events.

Rating an answer again appends a second event, so only each person's latest
rating of an answer counts. An event written before it named its agent is
attributed through its chat; one whose agent cannot be resolved is left out.
Same gate and rows as `GET /kpis?groupBy=agent`: `statistics:view` is
tenant-wide only, and a personal agent gets no row. One tenant never counts
another's ratings, nor names another's agent in a filter.
"""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.audit.chain import append_event
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="stats", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _get(tenant: uuid.UUID, query: str = "", role: str = "org_admin") -> tuple[int, object]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/api/v1/stats/feedback{query}", headers=_headers(tenant, role))
    return r.status_code, r.json()


async def _seed(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Agents Ada and Bo of "Ops", and Pia of a personal department; Ada's
    chat. Ratings:

    - answer 1: member X down, then up (counts up); member Y down
    - answer 2: member X up on model "big"
    - Bo, answer 3: member X down
    - answer 4: an old event in Ada's chat, no agent and no model: up
    - answer 5: an old event of a chat that is gone: down (left out)
    - Pia, answer 6: member X down (left out)

    Returns (Ada, Bo, Ops)."""
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        personal = m.Department(
            tenant_id=tenant,
            name="X",
            frame={"closed": True},
            kind="personal",
            owner_member_id=uuid.uuid4(),
        )
        db.add_all([dept, personal])
        await db.flush()
        ada = m.Agent(tenant_id=tenant, department_id=dept.id, name="Ada")
        bo = m.Agent(tenant_id=tenant, department_id=dept.id, name="Bo")
        pia = m.Agent(tenant_id=tenant, department_id=personal.id, name="Pia")
        db.add_all([ada, bo, pia])
        await db.flush()
        chat = m.ChatSession(tenant_id=tenant, agent_id=ada.id, member_id=uuid.uuid4())
        db.add(chat)
        await db.flush()
        x, y = uuid.uuid4(), uuid.uuid4()
        a1, a2, a3, a4, a5, a6 = (uuid.uuid4() for _ in range(6))

        async def rate(
            member: uuid.UUID,
            message: uuid.UUID,
            rating: str,
            agent: uuid.UUID | None = ada.id,
            model: str | None = "small",
            session: uuid.UUID = chat.id,
        ) -> None:
            resource: dict[str, object] = {
                "session_id": str(session),
                "message_id": str(message),
                "run_id": None,
                "rating": rating,
                "note": "",
            }
            if agent is not None:
                resource |= {"agent_id": str(agent), "department_id": str(dept.id), "model": model}
            await append_event(
                db,
                tenant_id=tenant,
                actor_type="operator",
                actor_id=member,
                category="chat",
                action="chat.feedback",
                resource=resource,
            )

        await rate(x, a1, "down")
        await rate(x, a1, "up")
        await rate(y, a1, "down")
        await rate(x, a2, "up", model="big")
        await rate(x, a3, "down", agent=bo.id)
        await rate(x, a4, "up", agent=None)
        await rate(x, a5, "down", agent=None, session=uuid.uuid4())
        await rate(x, a6, "down", agent=pia.id)
        return ada.id, bo.id, dept.id


async def test_counts_the_latest_rating_per_answer_and_person_by_agent_and_model(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    ada, bo, _ops = await _seed(app_session, tenant)
    status, body = await _get(tenant)
    assert status == 200, body
    assert body == [
        {"agentId": str(ada), "agentName": "Ada", "model": "big", "up": 1, "down": 0},
        {"agentId": str(ada), "agentName": "Ada", "model": "small", "up": 1, "down": 1},
        {"agentId": str(ada), "agentName": "Ada", "model": None, "up": 1, "down": 0},
        {"agentId": str(bo), "agentName": "Bo", "model": "small", "up": 0, "down": 1},
    ]


async def test_the_agent_filter_and_the_period_narrow_the_counts(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    _ada, bo, ops = await _seed(app_session, tenant)
    status, body = await _get(tenant, f"?agentId={bo}")
    assert status == 200, body
    assert body == [{"agentId": str(bo), "agentName": "Bo", "model": "small", "up": 0, "down": 1}]
    status, body = await _get(tenant, f"?departmentId={ops}")
    assert status == 200, body
    assert len(body) == 4
    status, body = await _get(tenant, "?dateTo=2000-01-01")
    assert (status, body) == (200, [])


async def test_one_tenant_never_counts_anothers_ratings(app_session: AppSessionFactory) -> None:
    other = uuid.uuid4()
    ada, _bo, ops = await _seed(app_session, other)
    mine = uuid.uuid4()
    await _seed(app_session, mine)
    status, body = await _get(mine)
    assert status == 200, body
    assert str(ada) not in {row["agentId"] for row in body}
    assert (await _get(mine, f"?agentId={ada}"))[0] == 404
    assert (await _get(mine, f"?departmentId={ops}"))[0] == 404
    assert (await _get(uuid.uuid4()))[1] == []


async def test_a_personal_agent_cannot_be_named_in_the_filter(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        pia = (await db.execute(select(m.Agent.id).where(m.Agent.name == "Pia"))).scalar_one()
    status, _body = await _get(tenant, f"?agentId={pia}")
    assert status == 404


async def test_an_agent_or_department_that_never_existed_is_a_404(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant)
    assert (await _get(tenant, f"?agentId={uuid.uuid4()}"))[0] == 404
    assert (await _get(tenant, f"?departmentId={uuid.uuid4()}"))[0] == 404


async def test_a_malformed_id_in_an_event_does_not_fail_the_count(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        await append_event(
            db,
            tenant_id=tenant,
            actor_type="operator",
            actor_id=uuid.uuid4(),
            category="chat",
            action="chat.feedback",
            resource={
                "session_id": "not-a-uuid",
                "agent_id": "also-not",
                "message_id": "x",
                "rating": "up",
            },
        )
    status, body = await _get(tenant)
    assert status == 200, body
    assert len(body) == 4


async def test_reading_the_counts_needs_statistics_view(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant)
    for role in ("member", "dept_manager"):
        status, _body = await _get(tenant, role=role)
        assert status == 403, role
