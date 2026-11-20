"""The My Work task board's two HTTP routes, at the level a browser sees them.

The scoping itself is proven in `tests/workspace/test_tasks.py` against the
module directly; these tests are only about the two things a route adds on
top -- the `RUN_START` gate `POST /tasks` layers over `AGENT_VIEW`, and that a
department with no team lead comes back as 409, not a silent no-op.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.tenants.activity import set_tenant_activity_gate
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append((run_id, tenant_id))


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    q = _FakeQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: q)
    return q


async def _department(
    db: Any, tenant: uuid.UUID, name: str, *, with_lead: bool
) -> m.Department:
    dept = m.Department(tenant_id=tenant, name=name, frame={})
    db.add(dept)
    await db.flush()
    if with_lead:
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name=f"{name}-Lead",
            status="idle",
            is_team_lead=True,
            narrowing={},
            definition={},
            presentation={},
        )
        db.add(lead)
        await db.flush()
        dept.team_lead_agent_id = lead.id
        await db.flush()
    return dept


async def test_creating_a_task_over_http_starts_a_run(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = await _department(db, tenant, "Kundenservice", with_lead=True)

    async with _http() as http:
        created = await http.post(
            "/api/v1/tasks",
            json={"departmentId": str(dept.id), "instructions": "Bitte Tickets zusammenfassen."},
            headers=_headers(tenant, "boss", "org_admin"),
        )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["departmentId"] == str(dept.id)
    assert body["column"] == "in_progress"
    assert len(queue.enqueued) == 1

    async with _http() as http:
        board = await http.get(
            "/api/v1/tasks", headers=_headers(tenant, "boss", "org_admin")
        )
    assert board.status_code == 200, board.text
    assert [t["id"] for t in board.json()] == [body["id"]]


async def test_a_department_without_a_team_lead_refuses_with_409(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = await _department(db, tenant, "Kundenservice", with_lead=False)

    async with _http() as http:
        refused = await http.post(
            "/api/v1/tasks",
            json={"departmentId": str(dept.id), "instructions": "Irgendwas erledigen."},
            headers=_headers(tenant, "boss", "org_admin"),
        )
    assert refused.status_code == 409, refused.text
    assert queue.enqueued == []


async def test_creating_a_task_for_an_inactive_organisation_answers_tenant_inactive(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = await _department(db, tenant, "Kundenservice", with_lead=True)

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    set_tenant_activity_gate(gate)
    async with _http() as http:
        refused = await http.post(
            "/api/v1/tasks",
            json={"departmentId": str(dept.id), "instructions": "Bitte Tickets zusammenfassen."},
            headers=_headers(tenant, "boss", "org_admin"),
        )
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "tenant_inactive"}
    assert queue.enqueued == []
    async with app_session(tenant) as db:
        tasks = (
            (await db.execute(select(m.Task).where(m.Task.department_id == dept.id)))
            .scalars()
            .all()
        )
        assert tasks == [], "nothing is written for a refused task"
