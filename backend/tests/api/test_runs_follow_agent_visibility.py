"""A run is visible exactly when its agent is.

Found in a browser E2E test: an Employee who got 403 on `GET /agents/{id}`
read the same agent's `GET /agents/{id}/runs` -- task excerpts, output
excerpts and failure reasons -- because every run route asked `run:view` and
the tenant and nothing else. The rule now: the caller may see the run's agent
(`visible_agent`'s rule: a tenant-wide `agent:view` read, or a seat in the
agent's department) AND holds the run permission tenant-wide or in that seat.
Detail routes answer 404 for "no such run" and "a run of an agent you may not
see" alike; lists narrow in the statement, before the limit.

`member` holds no `run:view` on this branch, so a seatless employee is refused
at the gate (`test_a_bare_employee_is_refused_at_the_gate`). The E2E's caller
held `run:view` without a tenant-wide `agent:view` read; the `employee_reads_runs`
fixture recreates exactly that by adding `run:view` to the `member` role for
one test, so the narrowing behind the gate -- the part that was missing -- is
what those tests exercise. `run:start`/`run:control` are delegatable today, so
the control routes are pinned with a real tenant-defined role instead.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import (
    BUILTIN_ROLE_PERMISSIONS,
    COPILOT_USE,
    MEMBER_ROLE,
    RUN,
    RUN_CONTROL,
    RUN_START,
    SEAT_VIEWER,
    VIEW,
    perm,
)
from oc8.authz.scope import subject_uuid_for
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)


def _headers(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@dataclass
class _Office:
    tenant: uuid.UUID
    sales: uuid.UUID
    engineering: uuid.UUID
    sales_agent: uuid.UUID
    engineering_agent: uuid.UUID
    archived_sales_agent: uuid.UUID
    assistant: uuid.UUID
    sales_run: uuid.UUID
    engineering_run: uuid.UUID
    archived_sales_run: uuid.UUID
    assistant_run: uuid.UUID
    #: A deleted agent in Engineering, and a run naming no agent row at all.
    archived_engineering_agent: uuid.UUID
    archived_engineering_run: uuid.UUID
    orphan_run: uuid.UUID
    #: A live `dept_viewer` seat in Sales only.
    sales_seat: str = "sales-employee"
    #: No seat anywhere, no assigned role.
    seatless: str = "seatless-employee"
    #: A tenant-defined role holding `run:start` + `run:control`, no seat.
    controller: str = "seatless-controller"
    #: The same tenant-defined role, with a `dept_viewer` seat in Sales.
    seated_controller: str = "sales-controller"
    #: Token `dept_manager`, seated in Sales: a department lead.
    lead: str = "sales-lead"


async def _agent(
    db: object,
    tenant: uuid.UUID,
    department: uuid.UUID,
    name: str,
    *,
    archived: bool = False,
    assistant: bool = False,
) -> uuid.UUID:
    agent = m.Agent(
        tenant_id=tenant,
        department_id=department,
        name=name,
        is_tenant_assistant=assistant,
        is_team_lead=assistant,
        deleted_at=T0 if archived else None,
    )
    db.add(agent)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return agent.id


async def _run(db: object, tenant: uuid.UUID, agent: uuid.UUID, task: str) -> uuid.UUID:
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent,
        state="waiting_for_input",
        source="manual",
        context={"task": task, "pending_question": "Which customer?", "steps": 1},
        created_at=T0,
    )
    db.add(run)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    # The open question the run waits on: /answer resolves an open
    # Clarification row, not just the run state.
    db.add(  # type: ignore[attr-defined]
        m.Clarification(tenant_id=tenant, run_id=run.id, agent_id=agent, question="Which customer?")
    )
    db.add(  # type: ignore[attr-defined]
        m.ActivityEvent(tenant_id=tenant, agent_id=agent, message=f"activity: {task}")
    )
    return run.id


async def _member(
    db: object,
    tenant: uuid.UUID,
    subject: str,
    *,
    seat_in: uuid.UUID | None = None,
    role_id: uuid.UUID | None = None,
) -> None:
    member = m.OrgMember(
        tenant_id=tenant, subject=subject, subject_uuid=subject_uuid_for(subject), role_id=role_id
    )
    db.add(member)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    if seat_in is not None:
        db.add(  # type: ignore[attr-defined]
            m.OrgMemberDepartment(
                tenant_id=tenant, member_id=member.id, department_id=seat_in, seat_role=SEAT_VIEWER
            )
        )
        await db.flush()  # type: ignore[attr-defined]


async def _office(app_session: AppSessionFactory) -> _Office:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sales = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        engineering = m.Department(tenant_id=tenant, name="Entwicklung", frame={})
        db.add_all([sales, engineering])
        await db.flush()

        sales_agent = await _agent(db, tenant, sales.id, "Nora")
        engineering_agent = await _agent(db, tenant, engineering.id, "Theo")
        archived_sales_agent = await _agent(db, tenant, sales.id, "Old Nora", archived=True)
        assistant = await _agent(db, tenant, sales.id, "Assistant", assistant=True)
        archived_engineering_agent = await _agent(
            db, tenant, engineering.id, "Old Theo", archived=True
        )

        office = _Office(
            tenant=tenant,
            sales=sales.id,
            engineering=engineering.id,
            sales_agent=sales_agent,
            engineering_agent=engineering_agent,
            archived_sales_agent=archived_sales_agent,
            assistant=assistant,
            sales_run=await _run(db, tenant, sales_agent, "Quote for Mueller GmbH"),
            engineering_run=await _run(db, tenant, engineering_agent, "Rotate the prod key"),
            archived_sales_run=await _run(db, tenant, archived_sales_agent, "Old quote"),
            assistant_run=await _run(db, tenant, assistant, "Chat with the CEO"),
            archived_engineering_agent=archived_engineering_agent,
            archived_engineering_run=await _run(
                db, tenant, archived_engineering_agent, "Old prod migration"
            ),
            orphan_run=await _run(db, tenant, uuid.uuid4(), "Nobody's run"),
        )
        # An event that names no agent at all.
        db.add(m.ActivityEvent(tenant_id=tenant, agent_id=None, message="activity: system"))

        controller_role = m.Role(tenant_id=tenant, name="Dispatcher", kind="human", builtin=False)
        db.add(controller_role)
        await db.flush()
        db.add_all(
            [
                m.RolePermission(tenant_id=tenant, role_id=controller_role.id, permission=p)
                for p in (RUN_START, RUN_CONTROL, COPILOT_USE)
            ]
        )
        await _member(db, tenant, office.sales_seat, seat_in=sales.id)
        await _member(db, tenant, office.seatless)
        await _member(db, tenant, office.controller, role_id=controller_role.id)
        await _member(
            db, tenant, office.seated_controller, seat_in=sales.id, role_id=controller_role.id
        )
        await _member(db, tenant, office.lead, seat_in=sales.id)
    return office


@pytest.fixture
def employee_reads_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The E2E caller: `run:view` held tenant-wide, `agent:view` not."""
    monkeypatch.setitem(
        BUILTIN_ROLE_PERMISSIONS, MEMBER_ROLE, frozenset({COPILOT_USE, perm(RUN, VIEW)})
    )


def _per_run_reads(run_id: uuid.UUID, agent_id: uuid.UUID) -> list[str]:
    return [
        f"/api/v1/agents/{agent_id}/runs?limit=20",
        f"/api/v1/agents/{agent_id}/workspace/files",
        f"/api/v1/runs/{run_id}",
    ]


async def test_a_bare_employee_is_refused_at_the_gate(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    h = _headers(office.tenant, office.seatless)
    async with _http() as http:
        for path in [
            *_per_run_reads(office.engineering_run, office.engineering_agent),
            "/api/v1/runs",
            "/api/v1/activity",
        ]:
            resp = await http.get(path, headers=h)
            assert resp.status_code == 403, (path, resp.text)
            assert "Rotate the prod key" not in resp.text


@pytest.mark.usefixtures("employee_reads_runs")
async def test_a_seatless_employee_gets_404_on_every_per_run_read(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    h = _headers(office.tenant, office.seatless)
    async with _http() as http:
        # The precondition the E2E saw: the agent itself is not hers to see.
        assert (
            await http.get(f"/api/v1/agents/{office.engineering_agent}", headers=h)
        ).status_code == 403
        for path in _per_run_reads(office.engineering_run, office.engineering_agent):
            resp = await http.get(path, headers=h)
            assert resp.status_code == 404, (path, resp.text)
            assert "Rotate the prod key" not in resp.text

        # And a run id that does not exist answers identically.
        missing = await http.get(f"/api/v1/runs/{uuid.uuid4()}", headers=h)
        foreign = await http.get(f"/api/v1/runs/{office.engineering_run}", headers=h)
        assert (missing.status_code, missing.json()) == (foreign.status_code, foreign.json())


@pytest.mark.usefixtures("employee_reads_runs")
async def test_a_seatless_employee_gets_empty_lists_and_feed(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    h = _headers(office.tenant, office.seatless)
    async with _http() as http:
        runs = await http.get("/api/v1/runs", headers=h)
        assert runs.status_code == 200, runs.text
        assert runs.json() == []

        feed = await http.get("/api/v1/activity", headers=h)
        assert feed.status_code == 200, feed.text
        assert feed.json() == []

        filtered = await http.get(
            "/api/v1/activity", params={"agentId": str(office.engineering_agent)}, headers=h
        )
        assert filtered.status_code == 200, filtered.text
        assert filtered.json() == []


@pytest.mark.usefixtures("employee_reads_runs")
async def test_a_sales_seated_employee_sees_sales_runs_only(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    h = _headers(office.tenant, office.sales_seat)
    async with _http() as http:
        runs = await http.get("/api/v1/runs", headers=h)
        assert runs.status_code == 200, runs.text
        # Her department's runs, the archived Sales agent's included; never
        # Engineering's, never the tenant Assistant's.
        assert {r["id"] for r in runs.json()} == {
            str(office.sales_run),
            str(office.archived_sales_run),
        }

        history = await http.get(f"/api/v1/agents/{office.sales_agent}/runs", headers=h)
        assert history.status_code == 200, history.text
        assert history.json()["totalCount"] == 1

        archived = await http.get(f"/api/v1/agents/{office.archived_sales_agent}/runs", headers=h)
        assert archived.status_code == 200, archived.text
        assert archived.json()["totalCount"] == 1

        assert (await http.get(f"/api/v1/runs/{office.sales_run}", headers=h)).status_code == 200

        for path in [
            *_per_run_reads(office.engineering_run, office.engineering_agent),
            *_per_run_reads(office.assistant_run, office.assistant),
            *_per_run_reads(office.archived_engineering_run, office.archived_engineering_agent),
            f"/api/v1/runs/{office.orphan_run}",
        ]:
            resp = await http.get(path, headers=h)
            assert resp.status_code == 404, (path, resp.text)

        feed = await http.get("/api/v1/activity", headers=h)
        assert feed.status_code == 200, feed.text
        assert sorted(e["message"] for e in feed.json()) == [
            "activity: Old quote",
            "activity: Quote for Mueller GmbH",
        ]


async def test_a_controller_cannot_act_on_runs_of_agents_she_cannot_see(
    app_session: AppSessionFactory,
) -> None:
    """`run:start`/`run:control` are delegatable: a composed role holding them
    and no seat used to cancel, answer and steer any run in the tenant by id."""
    office = await _office(app_session)
    h = _headers(office.tenant, office.controller)
    run_id = office.engineering_run
    async with _http() as http:
        for method, path, body in [
            ("POST", f"/api/v1/runs/{run_id}/answer", {"answer": "Acme"}),
            ("POST", f"/api/v1/runs/{run_id}/message", {"body": "stop"}),
            ("POST", f"/api/v1/runs/{run_id}/cancel", None),
            ("POST", f"/api/v1/agents/{office.engineering_agent}/run", {"task": "go"}),
        ]:
            resp = await http.request(method, path, headers=h, json=body)
            assert resp.status_code == 404, (path, resp.text)

    async with app_session(office.tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == "waiting_for_input"
        assert "clarifications" not in run.context


@pytest.mark.parametrize(
    ("role", "subject"), [("org_admin", "admin"), ("dept_manager", _Office.lead)]
)
async def test_org_admin_and_department_lead_still_see_everything(
    app_session: AppSessionFactory, role: str, subject: str
) -> None:
    office = await _office(app_session)
    h = _headers(office.tenant, subject, role=role)
    every_run = {
        str(office.sales_run),
        str(office.engineering_run),
        str(office.archived_sales_run),
        str(office.assistant_run),
        str(office.archived_engineering_run),
        str(office.orphan_run),
    }
    async with _http() as http:
        runs = await http.get("/api/v1/runs", headers=h)
        assert runs.status_code == 200, runs.text
        assert {r["id"] for r in runs.json()} == every_run

        for run_id, agent_id in [
            (office.engineering_run, office.engineering_agent),
            (office.assistant_run, office.assistant),
            (office.archived_sales_run, office.archived_sales_agent),
        ]:
            history = await http.get(f"/api/v1/agents/{agent_id}/runs", headers=h)
            assert history.status_code == 200, history.text
            assert history.json()["totalCount"] == 1
            assert (await http.get(f"/api/v1/runs/{run_id}", headers=h)).status_code == 200

        feed = await http.get("/api/v1/activity", headers=h)
        assert feed.status_code == 200, feed.text
        # Six runs' events (the orphan's included) plus the agentless one.
        assert len(feed.json()) == 7

        cancel = await http.post(f"/api/v1/runs/{office.engineering_run}/cancel", headers=h)
        assert cancel.status_code == 200, cancel.text


async def test_control_routes_answer_missing_and_foreign_identically(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    h = _headers(office.tenant, office.controller)
    async with _http() as http:
        for suffix, body in [
            ("answer", {"answer": "Acme"}),
            ("message", {"body": "stop"}),
            ("cancel", None),
        ]:
            foreign = await http.post(
                f"/api/v1/runs/{office.engineering_run}/{suffix}", headers=h, json=body
            )
            missing = await http.post(f"/api/v1/runs/{uuid.uuid4()}/{suffix}", headers=h, json=body)
            assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json())
            assert foreign.status_code == 404
        foreign_start = await http.post(
            f"/api/v1/agents/{office.engineering_agent}/run", headers=h, json={"task": "go"}
        )
        missing_start = await http.post(
            f"/api/v1/agents/{uuid.uuid4()}/run", headers=h, json={"task": "go"}
        )
        assert (foreign_start.status_code, foreign_start.json()) == (
            missing_start.status_code,
            missing_start.json(),
        )


@pytest.mark.usefixtures("redis_url")
async def test_a_seated_controller_acts_on_runs_in_her_own_department(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    h = _headers(office.tenant, office.seated_controller)
    run_id = office.sales_run
    async with _http() as http:
        message = await http.post(f"/api/v1/runs/{run_id}/message", headers=h, json={"body": "hi"})
        assert message.status_code == 202, message.text
        answer = await http.post(f"/api/v1/runs/{run_id}/answer", headers=h, json={"answer": "x"})
        assert answer.status_code == 200, answer.text
        cancel = await http.post(f"/api/v1/runs/{run_id}/cancel", headers=h)
        assert cancel.status_code == 200, cancel.text
        start = await http.post(
            f"/api/v1/agents/{office.sales_agent}/run", headers=h, json={"task": "go"}
        )
        assert start.status_code == 200, start.text
        # Still nothing outside Sales, the Assistant included.
        for agent in (office.engineering_agent, office.assistant):
            resp = await http.post(f"/api/v1/agents/{agent}/run", headers=h, json={"task": "go"})
            assert resp.status_code == 404, resp.text


async def _run_file(app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID) -> str:
    async with app_session(tenant) as db:
        row = m.FileAttachment(
            tenant_id=tenant,
            owner_type="agent_run",
            owner_id=run_id,
            bucket_key=f"test/{uuid.uuid4()}",
            filename="quote.pdf",
            content_type="application/pdf",
            size_bytes=3,
        )
        db.add(row)
        await db.flush()
        return str(row.id)


async def test_run_output_files_need_run_view_not_just_a_seat(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `dept_viewer` sees the Sales agent but holds no `run:view`: its run's
    output file is a 404, as `GET /runs/{id}` would be."""
    from oc8.storage import s3

    async def _bytes(key: str) -> bytes:
        return b"pdf"

    monkeypatch.setattr(s3, "get_object", _bytes)
    office = await _office(app_session)
    file_id = await _run_file(app_session, office.tenant, office.sales_run)
    async with _http() as http:
        seat = _headers(office.tenant, office.sales_seat)
        assert (
            await http.get(f"/api/v1/agents/{office.sales_agent}", headers=seat)
        ).status_code == 200
        refused = await http.get(f"/api/v1/files/{file_id}", headers=seat)
        assert refused.status_code == 404, refused.text
        missing = await http.get(f"/api/v1/files/{uuid.uuid4()}", headers=seat)
        assert refused.json() == missing.json()

        admin = _headers(office.tenant, "admin", role="org_admin")
        allowed = await http.get(f"/api/v1/files/{file_id}", headers=admin)
        assert allowed.status_code == 200, allowed.text


async def test_reports_follow_the_run_rule(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    component = {"componentKey": "bar_chart", "props": {"title": "Q"}}
    async with app_session(office.tenant) as db:
        for agent in (office.sales_agent, office.engineering_agent):
            db.add(
                m.AgentRun(
                    tenant_id=office.tenant,
                    agent_id=agent,
                    state="done",
                    context={"task": "report", "rendered_components": [component]},
                )
            )
    async with _http() as http:
        seat = await http.get("/api/v1/reports", headers=_headers(office.tenant, office.sales_seat))
        assert seat.status_code == 200, seat.text
        assert seat.json() == []

        admin = await http.get(
            "/api/v1/reports", headers=_headers(office.tenant, "admin", role="org_admin")
        )
        assert admin.status_code == 200, admin.text
        assert {r["agentId"] for r in admin.json()} == {
            str(office.sales_agent),
            str(office.engineering_agent),
        }


@pytest.mark.usefixtures("employee_reads_runs")
async def test_reports_for_a_run_reader_are_narrowed_to_her_departments(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    component = {"componentKey": "bar_chart", "props": {"title": "Q"}}
    async with app_session(office.tenant) as db:
        for agent in (office.sales_agent, office.engineering_agent):
            db.add(
                m.AgentRun(
                    tenant_id=office.tenant,
                    agent_id=agent,
                    state="done",
                    context={"task": "report", "rendered_components": [component]},
                )
            )
    async with _http() as http:
        resp = await http.get("/api/v1/reports", headers=_headers(office.tenant, office.sales_seat))
        assert resp.status_code == 200, resp.text
        assert [r["agentId"] for r in resp.json()] == [str(office.sales_agent)]
