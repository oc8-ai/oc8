"""Task 7 of the Design System Consistency plan: `DELETE /departments/{id}`
(hard-delete or archive, depending on dependents), `POST /departments/{id}/
restore`, and `GET /departments` widened to the uniform search/filter/group/
pagination contract (spec §1.1) -- `Page[DepartmentDTO]` instead of a bare
list.

Mirrors `tests/api/test_agents_archive_and_list_query.py` (Task 6) exactly in
shape and convention: no shared `client`/`tenant_headers` fixtures exist in
this suite, so each test builds its own `AsyncClient` against a fresh
`create_app()` and an `org_admin` bearer token (tenant-wide, so the
`department:manage` gate -- unlike Task 6's department-scoped `agent:write`
gate -- is never the thing under test here; see `test_department_patch.py`'s
own docstring: "Tenant-wide `department:manage` only ... no seat can ever
hold it").
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

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


async def test_delete_department_with_agents_archives(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora")
        db.add(agent)
        await db.flush()
        dept_id = dept.id

    async with _http() as http:
        resp = await http.delete(f"/api/v1/departments/{dept_id}", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "archived"

        default = await http.get("/api/v1/departments", headers=_headers(tenant))
        assert default.status_code == 200, default.text
        assert str(dept_id) not in [d["id"] for d in default.json()["items"]]

        archived = await http.get(
            "/api/v1/departments?includeArchived=true", headers=_headers(tenant)
        )
        assert archived.status_code == 200, archived.text
        assert str(dept_id) in [d["id"] for d in archived.json()["items"]]


async def test_delete_empty_department_hard_deletes(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Empty Dept", frame={})
        db.add(dept)
        await db.flush()
        dept_id = dept.id

    async with _http() as http:
        resp = await http.delete(f"/api/v1/departments/{dept_id}", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "deleted"

        archived = await http.get(
            "/api/v1/departments?includeArchived=true", headers=_headers(tenant)
        )
        assert str(dept_id) not in [d["id"] for d in archived.json()["items"]]


async def test_delete_department_with_agents_cascades_to_archive_the_agents_too(
    app_session: AppSessionFactory,
) -> None:
    """The department itself archives when it still has live agents -- and
    every one of those agents must be cascaded through the exact same
    hard-delete-or-archive decision `DELETE /agents/{id}` makes on its own
    (by its OWN run history), not blanket-archived regardless of it. An
    agent with no runs is actually removed; one with history is archived."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        no_history = m.Agent(tenant_id=tenant, department_id=dept.id, name="Fresh")
        with_history = m.Agent(tenant_id=tenant, department_id=dept.id, name="Veteran")
        db.add_all([no_history, with_history])
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=with_history.id, state="done")
        db.add(run)
        await db.flush()
        dept_id = dept.id
        no_history_id = no_history.id
        with_history_id = with_history.id

    async with _http() as http:
        resp = await http.delete(f"/api/v1/departments/{dept_id}", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "archived"

    async with app_session(tenant) as db:
        gone = await db.get(m.Agent, no_history_id)
        assert gone is None, "no-history agent is hard-deleted, mirroring DELETE /agents/{id}"
        archived = await db.get(m.Agent, with_history_id)
        assert archived is not None
        assert archived.deleted_at is not None


async def test_delete_department_stops_the_triggers_and_runs_of_its_agents(
    app_session: AppSessionFactory,
) -> None:
    """The cascade retires each agent exactly as `DELETE /agents/{id}` does:
    the archived agent's trigger is disabled and its queued run cancelled; the
    hard-deleted agent's trigger is deleted with it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        no_history = m.Agent(tenant_id=tenant, department_id=dept.id, name="Fresh")
        with_history = m.Agent(tenant_id=tenant, department_id=dept.id, name="Veteran")
        db.add_all([no_history, with_history])
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=with_history.id, context={}, state="queued")
        triggers = [
            m.Trigger(
                tenant_id=tenant,
                agent_id=agent.id,
                kind="cron",
                task_text="daily",
                cron_expression="0 9 * * *",
                enabled=True,
            )
            for agent in (no_history, with_history)
        ]
        db.add_all([run, *triggers])
        await db.flush()
        dept_id, run_id = dept.id, run.id
        orphan_id, kept_id = triggers[0].id, triggers[1].id

    async with _http() as http:
        resp = await http.delete(f"/api/v1/departments/{dept_id}", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "archived"

    async with app_session(tenant) as db:
        assert await db.get(m.Trigger, orphan_id) is None
        kept = await db.get(m.Trigger, kept_id)
        assert kept is not None
        assert kept.enabled is False
        cancelled = (
            await db.execute(
                select(m.RunCancellation.run_id).where(m.RunCancellation.run_id == run_id)
            )
        ).scalar_one_or_none()
        assert cancelled == run_id


async def test_delete_department_ignores_agents_already_archived(
    app_session: AppSessionFactory,
) -> None:
    """A department whose only agents are themselves already archived
    (`deleted_at IS NOT NULL`) has no live dependents and hard-deletes, same
    as `visible_agents`'s own default `include_archived=False` treats them as
    already gone."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Almost Empty", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Retired")
        db.add(agent)
        await db.flush()
        agent.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        dept_id = dept.id

    async with _http() as http:
        resp = await http.delete(f"/api/v1/departments/{dept_id}", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "deleted"


async def test_delete_bogus_department_404s(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant):
        pass

    async with _http() as http:
        resp = await http.delete(f"/api/v1/departments/{uuid.uuid4()}", headers=_headers(tenant))
        assert resp.status_code == 404, resp.text


async def test_restore_department(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Archived Dept", frame={})
        db.add(dept)
        await db.flush()
        dept.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        dept_id = dept.id

    async with _http() as http:
        resp = await http.post(f"/api/v1/departments/{dept_id}/restore", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == str(dept_id)

        default = await http.get("/api/v1/departments", headers=_headers(tenant))
        assert str(dept_id) in [d["id"] for d in default.json()["items"]]


async def test_restore_department_that_is_not_archived_404s(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Live Dept", frame={})
        db.add(dept)
        await db.flush()
        dept_id = dept.id

    async with _http() as http:
        resp = await http.post(f"/api/v1/departments/{dept_id}/restore", headers=_headers(tenant))
        assert resp.status_code == 404, resp.text


async def test_list_departments_paged(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add_all(
            [
                m.Department(tenant_id=tenant, name="Vertrieb", frame={}),
                m.Department(tenant_id=tenant, name="Support", frame={}),
                m.Department(tenant_id=tenant, name="Engineering", frame={}),
            ]
        )
        await db.flush()

    async with _http() as http:
        resp = await http.get("/api/v1/departments?limit=1", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "totalCount" in body
        assert body["totalCount"] == 3
        assert len(body["items"]) <= 1


async def test_list_departments_search(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add_all(
            [
                m.Department(tenant_id=tenant, name="Vertrieb", frame={}),
                m.Department(tenant_id=tenant, name="Support", frame={}),
            ]
        )
        await db.flush()

    async with _http() as http:
        resp = await http.get("/api/v1/departments?search=vertrieb", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["totalCount"] == 1
        assert body["items"][0]["name"] == "Vertrieb"


async def test_list_departments_default_page_shape_has_total_count(
    app_session: AppSessionFactory,
) -> None:
    """The default (unfiltered) list is now `Page[DepartmentDTO]`, not a bare
    list -- pinned separately from the search/pagination test above so a
    regression to the old bare-list shape fails even when nobody filters or
    paginates."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Department(tenant_id=tenant, name="Solo", frame={}))
        await db.flush()

    async with _http() as http:
        resp = await http.get("/api/v1/departments", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["totalCount"] == 1
        assert body["items"][0]["name"] == "Solo"
