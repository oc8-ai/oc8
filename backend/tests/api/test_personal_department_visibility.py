"""What colleagues can see of a personal department.

By id (ruling S1): a personal department is visible to its owner and to a
caller who holds tenant-wide `department:manage`; tenant-wide read alone is not
enough.

In lists (ruling 8): a personal department is never listed, and its agents are
listed only to a caller seated there -- its owner -- even when the caller reads
every department tenant-wide or holds `all_departments`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER, SEAT_VIEWER
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SEATED_IN_SHARED = ("colleague", "ceo", "operator", "auditor", "manager", "admin")


@dataclass(frozen=True)
class _Tenant:
    id: uuid.UUID
    shared: uuid.UUID
    personal: uuid.UUID
    nora: uuid.UUID
    pia: uuid.UUID


def _subject_uuid(subject: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _setup(app_session: AppSessionFactory) -> _Tenant:
    """A standard "Vertrieb" with agent Nora, and `owner`'s personal department
    with agent Pia. `owner` sits only in the personal one, as `dept_approver`
    with `agent_manage`; every other subject sits in Vertrieb as a viewer.
    `ceo` also holds `all_departments`; its Vertrieb seat is what puts rows in
    its lists at all (`all_departments` widens approvals, not agent reads), so
    an empty list cannot pass the exclusion tests by accident."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        shared = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(shared)
        members = {}
        for subject in ("owner", *_SEATED_IN_SHARED):
            member = m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=_subject_uuid(subject),
                all_departments=subject == "ceo",
            )
            db.add(member)
            members[subject] = member
        await db.flush()
        personal = m.Department(
            tenant_id=tenant,
            name="Owner",
            frame={"closed": True},
            kind="personal",
            owner_member_id=members["owner"].id,
        )
        db.add(personal)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=members["owner"].id,
                department_id=personal.id,
                seat_role=SEAT_APPROVER,
                agent_manage=True,
            )
        )
        for subject in _SEATED_IN_SHARED:
            db.add(
                m.OrgMemberDepartment(
                    tenant_id=tenant,
                    member_id=members[subject].id,
                    department_id=shared.id,
                    seat_role=SEAT_VIEWER,
                )
            )
        nora = m.Agent(tenant_id=tenant, department_id=shared.id, name="Nora")
        pia = m.Agent(tenant_id=tenant, department_id=personal.id, name="Pia")
        db.add_all([nora, pia])
        await db.flush()
        return _Tenant(id=tenant, shared=shared.id, personal=personal.id, nora=nora.id, pia=pia.id)


@pytest.mark.parametrize(
    ("subject", "role", "status"),
    [
        ("owner", "member", 200),
        ("admin", "org_admin", 200),
        ("operator", "operator", 404),
        ("auditor", "auditor", 404),
        ("manager", "dept_manager", 200),
        ("colleague", "member", 404),
    ],
)
async def test_a_personal_department_is_visible_by_id_only_to_owner_and_manager(
    app_session: AppSessionFactory, subject: str, role: str, status: int
) -> None:
    t = await _setup(app_session)
    async with _http() as http:
        r = await http.get(
            f"/api/v1/departments/{t.personal}", headers=_headers(t.id, subject, role)
        )
    assert r.status_code == status, r.text


async def test_a_member_colleague_never_sees_it(app_session: AppSessionFactory) -> None:
    t = await _setup(app_session)
    h = _headers(t.id, "colleague", "member")
    async with _http() as http:
        depts = await http.get("/api/v1/departments", headers=h)
        assert depts.status_code == 200, depts.text
        assert [d["name"] for d in depts.json()["items"]] == ["Vertrieb"]

        agents = await http.get("/api/v1/agents", headers=h)
        assert agents.status_code == 200, agents.text
        assert [a["name"] for a in agents.json()["items"]] == ["Nora"]

        for path in (
            f"/api/v1/departments/{t.personal}",
            f"/api/v1/agents/{t.pia}",
            f"/api/v1/departments/{t.personal}/agents",
        ):
            r = await http.get(path, headers=h)
            assert r.status_code == 404, (path, r.text)


async def test_the_owner_lists_their_own_agents_but_not_the_department(
    app_session: AppSessionFactory,
) -> None:
    t = await _setup(app_session)
    h = _headers(t.id, "owner", "member")
    async with _http() as http:
        agents = await http.get("/api/v1/agents", headers=h)
        assert agents.status_code == 200, agents.text
        assert str(t.pia) in [a["id"] for a in agents.json()["items"]]

        depts = await http.get("/api/v1/departments", headers=h)
        assert depts.status_code == 200, depts.text
        assert str(t.personal) not in [d["id"] for d in depts.json()["items"]]

        own = await http.get(f"/api/v1/departments/{t.personal}", headers=h)
        assert own.status_code == 200, own.text
        assert own.json()["kind"] == "personal"

        shared = await http.get(
            f"/api/v1/departments/{t.shared}", headers=_headers(t.id, "colleague", "member")
        )
        assert shared.status_code == 200, shared.text
        assert shared.json()["kind"] == "standard"


@pytest.mark.parametrize(
    ("subject", "role"),
    [("admin", "org_admin"), ("ceo", "member"), ("manager", "dept_manager")],
)
async def test_tenant_wide_and_all_departments_lists_exclude_it(
    app_session: AppSessionFactory, subject: str, role: str
) -> None:
    t = await _setup(app_session)
    h = _headers(t.id, subject, role)
    async with _http() as http:
        depts = await http.get("/api/v1/departments", headers=h)
        assert depts.status_code == 200, depts.text
        dept_ids = [d["id"] for d in depts.json()["items"]]
        assert str(t.shared) in dept_ids, "the standard department must still be listed"
        assert str(t.personal) not in dept_ids

        agents = await http.get("/api/v1/agents", headers=h)
        assert agents.status_code == 200, agents.text
        assert agents.json()["totalCount"] == len(agents.json()["items"]), "counts stay true"
        agent_ids = [a["id"] for a in agents.json()["items"]]
        assert str(t.nora) in agent_ids, "the standard agent must still be listed"
        assert str(t.pia) not in agent_ids

        by_dept = await http.get(
            "/api/v1/agents", params={"departmentId": str(t.personal)}, headers=h
        )
        assert by_dept.status_code == 200, by_dept.text
        assert by_dept.json()["items"] == []


@pytest.mark.parametrize(
    ("subject", "role", "status"),
    [("admin", "org_admin", 200), ("manager", "dept_manager", 403)],
)
async def test_the_tenant_kpi_breakdowns_have_no_personal_rows(
    app_session: AppSessionFactory, subject: str, role: str, status: int
) -> None:
    """`dept_manager` holds `department:manage` (so it reads a personal
    department by id, S1) but not `statistics:view`: the tenant breakdown is
    closed to it entirely, which is pinned here so granting it later has to
    come back through this test."""
    t = await _setup(app_session)
    h = _headers(t.id, subject, role)
    async with _http() as http:
        for group_by, present, absent in (
            ("department", t.shared, t.personal),
            ("agent", t.nora, t.pia),
        ):
            r = await http.get("/api/v1/kpis", params={"groupBy": group_by}, headers=h)
            assert r.status_code == status, r.text
            if status != 200:
                continue
            keys = [row["groupKey"] for row in r.json()["rows"]]
            assert str(present) in keys
            assert str(absent) not in keys


@pytest.mark.parametrize(
    ("subject", "role", "status"),
    [
        ("owner", "member", 200),
        ("admin", "org_admin", 200),
        ("manager", "dept_manager", 200),
        ("operator", "operator", 404),
        ("auditor", "auditor", 404),
        ("colleague", "member", 404),
    ],
)
async def test_a_personal_agent_is_readable_by_id_only_like_its_department(
    app_session: AppSessionFactory, subject: str, role: str, status: int
) -> None:
    """S2: `visible_agent(id)` applies S1 to an agent in a personal department.
    `operator` and `auditor` read every agent tenant-wide, and still get a 404."""
    t = await _setup(app_session)
    h = _headers(t.id, subject, role)
    async with _http() as http:
        paths = [f"/api/v1/agents/{t.pia}", f"/api/v1/agents/{t.pia}/kpis"]
        if role != "member":
            # A `member` seat carries no `agent_version:view`; that gate's 403
            # comes before any row is read, so only the built-ins reach it.
            # `draft-status` and not `versions`: a version is content, which a
            # manager never reads (test_personal_agent_content.py); this pins
            # the reach rule alone.
            paths.append(f"/api/v1/agents/{t.pia}/draft-status")
        for path in paths:
            r = await http.get(path, headers=h)
            assert r.status_code == status, (path, r.text)


async def test_an_operator_cannot_read_a_personal_agents_run_by_id(
    app_session: AppSessionFactory,
) -> None:
    """The plan's "Known gap", closed in PR 3: `operator` holds `run:view`
    tenant-wide, so `run_reach(...).every_agent` used to reach a personal
    agent's run; `RunReach.private` now takes it out of that branch too."""
    t = await _setup(app_session)
    async with app_session(t.id) as db:
        run = m.AgentRun(tenant_id=t.id, agent_id=t.pia, state="done", context={})
        db.add(run)
        await db.flush()
        run_id = run.id
    async with _http() as http:
        r = await http.get(f"/api/v1/runs/{run_id}", headers=_headers(t.id, "operator", "operator"))
    assert r.status_code == 404, r.text
