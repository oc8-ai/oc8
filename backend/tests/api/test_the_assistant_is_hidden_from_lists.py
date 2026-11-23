"""The oc8 Assistant and the department it lives in are auto-provisioned
plumbing, not something anybody configures -- so they must not appear on any
screen a person browses agents or departments on (the Office floor, the
Departments list, the Agents list), while everything the Assistant itself needs
keeps working.

Every one of those screens reads through exactly `agents.repo.visible_agent(s)`
and `departments.repo.visible_department(s)` -- that is what
`tests/agents/test_reads_go_through_the_scoped_repository.py` enforces -- so
the filter lives in those two modules and this file checks the doors.

`GET /kpis?groupBy=...` is the exception that has to be checked separately: it
enumerates ids with its own `select(Agent.id)`/`select(Department.id)` rather
than reading rows through either funnel, so it needs -- and now carries -- the
same two flag terms.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="boss", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _ordinary_department(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    """One real department with one real agent, so an empty list would not pass
    these tests by accident."""
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        db.add(m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora"))
        await db.flush()
        return dept.id


async def test_the_assistant_and_its_department_are_absent_from_both_lists(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    ordinary_dept = await _ordinary_department(app_session, tenant)

    async with _http() as http:
        # GET /assistant is what lazily provisions the pair in the first place.
        assistant_r = await http.get("/api/v1/assistant", headers=_headers(tenant))
        assert assistant_r.status_code == 200, assistant_r.text
        assistant_id = assistant_r.json()["agentId"]

        agents_r = await http.get("/api/v1/agents", headers=_headers(tenant))
        assert agents_r.status_code == 200, agents_r.text
        agent_ids = [a["id"] for a in agents_r.json()["items"]]
        assert assistant_id not in agent_ids
        assert agent_ids, "the ordinary agent must still be listed"

        depts_r = await http.get("/api/v1/departments", headers=_headers(tenant))
        assert depts_r.status_code == 200, depts_r.text
        dept_names = [d["name"] for d in depts_r.json()["items"]]
        assert "oc8 Assistant" not in dept_names
        assert "Vertrieb" in dept_names

    async with app_session(tenant) as db:
        assistant = await db.get(m.Agent, uuid.UUID(assistant_id))
        assert assistant is not None, "still provisioned, just not listed"
        dept = await db.get(m.Department, assistant.department_id)
        assert dept is not None
        assert dept.kind == "assistant"
        assert dept.id != ordinary_dept


async def test_the_assistants_own_detail_routes_404_like_anything_else_hidden(
    app_session: AppSessionFactory,
) -> None:
    """Hidden means hidden, not "listed nowhere but still fetchable by id" --
    otherwise a screen could still link to it and half-work."""
    tenant = uuid.uuid4()
    async with _http() as http:
        assistant_id = (await http.get("/api/v1/assistant", headers=_headers(tenant))).json()[
            "agentId"
        ]
        detail = await http.get(f"/api/v1/agents/{assistant_id}", headers=_headers(tenant))
        assert detail.status_code == 404, detail.text

    async with app_session(tenant) as db:
        assistant = await db.get(m.Agent, uuid.UUID(assistant_id))
        assert assistant is not None
        department_id = assistant.department_id

    async with _http() as http:
        dept_detail = await http.get(
            f"/api/v1/departments/{department_id}", headers=_headers(tenant)
        )
        assert dept_detail.status_code == 404, dept_detail.text


async def test_the_assistant_is_not_a_group_row_in_the_tenant_kpi_breakdown(
    app_session: AppSessionFactory,
) -> None:
    """`GET /kpis?groupBy=agent|department` builds its own id query
    (`kpis._group_id_query`) instead of going through the two funnels, so
    hiding the Assistant from the lists left it enumerated here: a breakdown
    row for an agent whose own `/agents/{id}` and per-agent KPI routes 404.
    A row nobody can click through is worse than no row."""
    tenant = uuid.uuid4()
    ordinary_dept = await _ordinary_department(app_session, tenant)

    async with _http() as http:
        assistant_id = (await http.get("/api/v1/assistant", headers=_headers(tenant))).json()[
            "agentId"
        ]

        by_agent = await http.get(
            "/api/v1/kpis", params={"groupBy": "agent"}, headers=_headers(tenant)
        )
        assert by_agent.status_code == 200, by_agent.text
        agent_keys = [row["groupKey"] for row in by_agent.json()["rows"]]
        assert assistant_id not in agent_keys
        assert agent_keys, "the ordinary agent must still get a row"

        by_department = await http.get(
            "/api/v1/kpis", params={"groupBy": "department"}, headers=_headers(tenant)
        )
        assert by_department.status_code == 200, by_department.text
        dept_keys = [row["groupKey"] for row in by_department.json()["rows"]]
        assert str(ordinary_dept) in dept_keys, "the ordinary department must still get a row"

    async with app_session(tenant) as db:
        assistant = await db.get(m.Agent, uuid.UUID(assistant_id))
        assert assistant is not None
        assert str(assistant.department_id) not in dept_keys


async def test_the_assistants_own_chat_surface_still_works_end_to_end(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The one door that IS about the Assistant must not be closed by hiding it
    everywhere else: `POST /chat/sessions` goes through the same
    `visible_agent` funnel, and opts in explicitly once `_assistant_visible`
    has established that this caller holds copilot:manage and that this id is
    the tenant's Assistant."""
    tenant = uuid.uuid4()
    async with _http() as http:
        assistant_id = (await http.get("/api/v1/assistant", headers=_headers(tenant))).json()[
            "agentId"
        ]
        create_r = await http.post(
            "/api/v1/chat/sessions",
            json={"agentId": assistant_id},
            headers=_headers(tenant),
        )
        assert create_r.status_code == 201, create_r.text
        session_id = create_r.json()["id"]

        send_r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"message": "Wie viele offene Tickets?"},
            headers=_headers(tenant),
        )
        assert send_r.status_code == 201, send_r.text
