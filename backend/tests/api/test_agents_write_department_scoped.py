"""Department-scoped agent WRITE authority: `require_agent_write()` (the coarse
door) and `authorize_agent_write(...)` (the per-resource narrow), across all six
routes decision 1 names -- `POST /agents` plus the five `agents_write.py`
mutations.

Two things this file exists to pin, and the reason it is reviewed harder than
anything else in this slice:

* **The ordering rule that closes the frame-reconstruction leak.** A wrong-
  department toggle holder must be refused with the SAME status and text the
  route already gives a genuinely missing resource -- never a 422 carrying
  `narrowing_within_frame`'s or `missing_skill_requirements`'s per-tool
  violation list, which is the target department's tool frame, reconstructable
  one crafted request at a time. `authorize_agent_write` has to run BEFORE any
  of that, not merely alongside it.
* **The toggle is additive, not substitutive.** A live seat in the right
  department with no toggle must be refused with 403 (a permission the caller
  simply does not hold), never 404 (which would say "there is nothing here for
  you to be refused from" about a department the caller can plainly see).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_VIEWER
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _subject_uuid(subject: str) -> uuid.UUID:
    try:
        return uuid.UUID(subject)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


# ------------------------------------------------------------------- the office


@dataclass
class _Office:
    tenant: uuid.UUID
    home: uuid.UUID  # Vertrieb -- the toggle holders' own department
    foreign: uuid.UUID  # Entwicklung -- the department they hold nothing in
    home_agent: uuid.UUID
    foreign_agent: uuid.UUID
    model_config_id: uuid.UUID
    skill_version_id: uuid.UUID
    #: `agent_manage=True` in `home` only. No tenant-wide grant (token role
    #: `member`, which holds the empty set).
    toggle_subject: str = "toggle-holder"
    #: A live `dept_viewer` seat in `home`, `agent_manage=False`. The (d) case.
    plain_seat_subject: str = "plain-seat"


async def _office(app_session: AppSessionFactory) -> _Office:
    """Two departments, an agent already living in each, and the tenant-wide
    catalog rows (a model config, a skill version with no requirements) every
    write route needs to succeed at all -- so a 403/404 in these tests is never
    mistaken for "the fixture is incomplete"."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        home = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        foreign = m.Department(tenant_id=tenant, name="Entwicklung", frame={})
        db.add_all([home, foreign])
        await db.flush()

        home_agent = m.Agent(
            tenant_id=tenant,
            department_id=home.id,
            name="Nora",
            status="stopped",
            narrowing={},
            definition={},
            presentation={},
        )
        foreign_agent = m.Agent(
            tenant_id=tenant,
            department_id=foreign.id,
            name="Theo",
            status="stopped",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add_all([home_agent, foreign_agent])
        await db.flush()

        model_config = m.ModelConfig(
            tenant_id=tenant, provider="ollama", model="stub", locality="local"
        )
        db.add(model_config)
        await db.flush()

        skill = m.Skill(tenant_id=tenant, name="S", origin="local", trust_level="first_party")
        db.add(skill)
        await db.flush()
        version = m.SkillVersion(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={"requires": {}},
            artifact_hash=uuid.uuid4().bytes,
        )
        db.add(version)
        await db.flush()
        skill.current_version_id = version.id
        await db.flush()

        office = _Office(
            tenant=tenant,
            home=home.id,
            foreign=foreign.id,
            home_agent=home_agent.id,
            foreign_agent=foreign_agent.id,
            model_config_id=model_config.id,
            skill_version_id=version.id,
        )

        toggle = m.OrgMember(
            tenant_id=tenant,
            subject=office.toggle_subject,
            subject_uuid=_subject_uuid(office.toggle_subject),
        )
        plain = m.OrgMember(
            tenant_id=tenant,
            subject=office.plain_seat_subject,
            subject_uuid=_subject_uuid(office.plain_seat_subject),
        )
        db.add_all([toggle, plain])
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=toggle.id,
                department_id=home.id,
                seat_role=SEAT_VIEWER,
                agent_manage=True,
            )
        )
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=plain.id,
                department_id=home.id,
                seat_role=SEAT_VIEWER,
                agent_manage=False,
            )
        )
        await db.flush()
    return office


# ------------------------------------------------------------------- the routes

Request = Callable[[_Office, str], tuple[str, str, dict[str, Any]]]


def _create_agent(office: _Office, department: str) -> tuple[str, str, dict[str, Any]]:
    dept_id = office.home if department == "home" else office.foreign
    return (
        "POST",
        "/api/v1/agents",
        {
            "name": f"N-{uuid.uuid4().hex[:8]}",
            "departmentId": str(dept_id),
        },
    )


def _agent_id(office: _Office, department: str) -> uuid.UUID:
    return office.home_agent if department == "home" else office.foreign_agent


def _lifecycle(office: _Office, department: str) -> tuple[str, str, dict[str, Any]]:
    return "POST", f"/api/v1/agents/{_agent_id(office, department)}/lifecycle", {"action": "start"}


def _narrowing(office: _Office, department: str) -> tuple[str, str, dict[str, Any]]:
    return "PUT", f"/api/v1/agents/{_agent_id(office, department)}/narrowing", {"narrowing": {}}


def _runtime(office: _Office, department: str) -> tuple[str, str, dict[str, Any]]:
    # `null` clears the runtime -- the one shape that needs no runtime plugin
    # fixture to succeed, so the test is about the department gate and nothing else.
    return (
        "PUT",
        f"/api/v1/agents/{_agent_id(office, department)}/runtime",
        {"runtimePluginId": None},
    )


def _model_config(office: _Office, department: str) -> tuple[str, str, dict[str, Any]]:
    return (
        "PATCH",
        f"/api/v1/agents/{_agent_id(office, department)}/model-config",
        {"modelConfigId": str(office.model_config_id)},
    )


def _skills(office: _Office, department: str) -> tuple[str, str, dict[str, Any]]:
    return (
        "POST",
        f"/api/v1/agents/{_agent_id(office, department)}/skills",
        {"skillVersionId": str(office.skill_version_id)},
    )


def _autonomy(office: _Office, department: str) -> tuple[str, str, dict[str, Any]]:
    # E2E F2: how far an agent acts unasked is an agent write like the rest.
    return (
        "PUT",
        f"/api/v1/agents/{_agent_id(office, department)}/autonomy",
        {"autonomy": "autonomous", "grants": ["outward"]},
    )


#: The six routes decision 1 names (and the autonomy switch since), each with
#: the request that makes it succeed and the status/text the route already
#: gives a genuinely missing resource.
_MUTATIONS: dict[str, dict[str, Any]] = {
    "create_agent": {
        "request": _create_agent,
        "not_found_status": 400,
        "not_found_text": "unknown department",
    },
    "lifecycle": {
        "request": _lifecycle,
        "not_found_status": 404,
        "not_found_text": "agent not found",
    },
    "narrowing": {
        "request": _narrowing,
        "not_found_status": 404,
        "not_found_text": "agent not found",
    },
    "runtime": {
        "request": _runtime,
        "not_found_status": 404,
        "not_found_text": "agent not found",
    },
    "model_config": {
        "request": _model_config,
        "not_found_status": 404,
        "not_found_text": "agent not found",
    },
    "skills": {
        "request": _skills,
        "not_found_status": 404,
        "not_found_text": "agent not found",
    },
    "autonomy": {
        "request": _autonomy,
        "not_found_status": 404,
        "not_found_text": "agent not found",
    },
}


# ---------------------------------------------------------------------- test 6


@pytest.mark.parametrize("route_key", sorted(_MUTATIONS))
async def test_a_tenant_wide_holder_is_unaffected(
    app_session: AppSessionFactory, route_key: str
) -> None:
    """(a) The `require_agent_write` tenant-wide bypass, pinned as a regression:
    scoping write authority to a department must not narrow what `org_admin`
    (tenant-wide `agent:manage`) could already do everywhere."""
    office = await _office(app_session)
    spec = _MUTATIONS[route_key]
    async with _http() as http:
        for department in ("home", "foreign"):
            method, url, body = spec["request"](office, department)
            r = await http.request(
                method, url, json=body, headers=_headers(office.tenant, "boss", "org_admin")
            )
            assert r.status_code < 300, f"{route_key}/{department}: {r.status_code} {r.text}"


@pytest.mark.parametrize("route_key", sorted(_MUTATIONS))
async def test_b_own_department_toggle_holder_is_admitted(
    app_session: AppSessionFactory, route_key: str
) -> None:
    """(b) The department-scoped toggle actually admits, in its own department,
    a caller who holds no tenant-wide grant at all."""
    office = await _office(app_session)
    spec = _MUTATIONS[route_key]
    method, url, body = spec["request"](office, "home")
    async with _http() as http:
        r = await http.request(
            method, url, json=body, headers=_headers(office.tenant, office.toggle_subject)
        )
        assert r.status_code < 300, f"{route_key}: {r.status_code} {r.text}"


@pytest.mark.parametrize("route_key", sorted(_MUTATIONS))
async def test_c_other_department_toggle_holder_is_refused_like_a_missing_resource(
    app_session: AppSessionFactory, route_key: str
) -> None:
    """(c) The narrow half. A toggle good only in `home` must be refused,
    reaching for `foreign`, with EXACTLY the status and text the route gives a
    resource that does not exist -- never a 422 carrying `foreign`'s frame."""
    office = await _office(app_session)
    spec = _MUTATIONS[route_key]
    method, url, body = spec["request"](office, "foreign")
    async with _http() as http:
        r = await http.request(
            method, url, json=body, headers=_headers(office.tenant, office.toggle_subject)
        )
        assert r.status_code == spec["not_found_status"], f"{route_key}: {r.status_code} {r.text}"
        assert spec["not_found_text"] in r.text, r.text


@pytest.mark.parametrize("route_key", sorted(_MUTATIONS))
async def test_d_own_department_seat_without_the_toggle_is_refused_with_403(
    app_session: AppSessionFactory, route_key: str
) -> None:
    """(d) A live seat, in the right department, WITHOUT the toggle, is a
    permission the caller does not hold -- 403, never 404. 404 there would claim
    the department or agent does not exist, when the caller can plainly see it."""
    office = await _office(app_session)
    spec = _MUTATIONS[route_key]
    method, url, body = spec["request"](office, "home")
    async with _http() as http:
        r = await http.request(
            method, url, json=body, headers=_headers(office.tenant, office.plain_seat_subject)
        )
        assert r.status_code == 403, f"{route_key}: {r.status_code} {r.text}"


@pytest.mark.parametrize("route_key", sorted(_MUTATIONS))
async def test_e_org_admin_with_no_seat_anywhere_still_succeeds(
    app_session: AppSessionFactory, route_key: str
) -> None:
    """(e) Distinct from (a): here the caller holds NO seat row at all -- not even
    a revoked one -- so `require_agent_write`'s tenant-wide branch is the only
    thing that can be admitting him, on a department he was never seated in."""
    office = await _office(app_session)
    spec = _MUTATIONS[route_key]
    method, url, body = spec["request"](office, "foreign")
    async with _http() as http:
        r = await http.request(
            method, url, json=body, headers=_headers(office.tenant, "unseated-admin", "org_admin")
        )
        assert r.status_code < 300, f"{route_key}: {r.status_code} {r.text}"

    async with app_session(office.tenant) as db:
        seats = (
            (
                await db.execute(
                    select(m.OrgMemberDepartment)
                    .join(m.OrgMember, m.OrgMember.id == m.OrgMemberDepartment.member_id)
                    .where(m.OrgMember.subject == "unseated-admin")
                )
            )
            .scalars()
            .all()
        )
    assert seats == [], "the admin holds a seat row -- the success above proves nothing"


# ------------------------------------------------------------------ tests 7 & 8


async def test_create_agent_does_not_leak_foreign_department_frame_via_narrowing_violations(
    app_session: AppSessionFactory,
) -> None:
    """A Sales-only toggle holder POSTs `/agents` naming Engineering and an
    oversized narrowing (Engineering's frame is empty, so any enabled tool is a
    violation). The refusal must be the plain 400 `authorize_agent_write` gives
    for ANY unauthorized department -- never `narrowing_within_frame`'s 422,
    whose body is Engineering's tool frame, one violation at a time."""
    office = await _office(app_session)
    async with _http() as http:
        r = await http.post(
            "/api/v1/agents",
            json={
                "name": "Should not exist",
                "departmentId": str(office.foreign),
                "narrowing": {"tools": {"secret_crm": {"enabled": True, "read": True}}},
            },
            headers=_headers(office.tenant, office.toggle_subject),
        )
        assert r.status_code == 400, r.text
        assert "unknown department" in r.text
        assert "secret_crm" not in r.text, "the refusal body leaked the target frame's tool name"
        assert "violation" not in r.text.lower(), r.text

    async with app_session(office.tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.Agent).where(
                        m.Agent.tenant_id == office.tenant, m.Agent.name == "Should not exist"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == [], "the refused request still created an agent"


async def test_assign_skill_does_not_leak_foreign_department_frame_via_requirement_violations(
    app_session: AppSessionFactory,
) -> None:
    """Same class of leak, through `missing_skill_requirements`'s 422 instead of
    `narrowing_within_frame`'s. The skill here requires a tool Engineering's
    (empty) frame does not grant; the refusal must be the plain 404 the route
    already gives a missing agent, not the 422 naming the missing tool."""
    office = await _office(app_session)
    async with app_session(office.tenant) as db:
        skill = m.Skill(
            tenant_id=office.tenant,
            name="Needs a secret tool",
            origin="local",
            trust_level="first_party",
        )
        db.add(skill)
        await db.flush()
        secret_version = m.SkillVersion(
            tenant_id=office.tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={"requires": {"tools": [{"tool": "secret_crm", "rights": ["read"]}]}},
            artifact_hash=uuid.uuid4().bytes,
        )
        db.add(secret_version)
        await db.flush()
        skill.current_version_id = secret_version.id
        await db.flush()
        secret_version_id = secret_version.id

    async with _http() as http:
        r = await http.post(
            f"/api/v1/agents/{office.foreign_agent}/skills",
            json={"skillVersionId": str(secret_version_id)},
            headers=_headers(office.tenant, office.toggle_subject),
        )
        assert r.status_code == 404, r.text
        assert "agent not found" in r.text
        assert "secret_crm" not in r.text, "the refusal body leaked the target frame's tool name"
        assert "missing" not in r.text.lower(), r.text

    async with app_session(office.tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.SkillAssignment).where(
                        m.SkillAssignment.agent_id == office.foreign_agent
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == [], "the refused request still assigned the skill"
