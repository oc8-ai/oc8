"""The generic agent routes hold a personal department to its frame and cap.

The owner of a personal department holds `agent_manage` on it, so `POST
/agents`, the narrowing routes and `restore_agent` reach it like any other
department. Each of them must refuse what the member hire route refuses: a tool
outside the personal frame, and an agent beyond the tenant's cap. Standard
departments are unaffected. Every test mints its own tenant.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.pdp import effective_tool_policies
from oc8.authz.scope import subject_uuid_for
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin, instantiate_agent
from oc8.copilot.profile import offboard_member
from oc8.departments.personal import (
    ENABLED_KEY,
    FRAME_KEY,
    MAX_AGENTS_KEY,
    PersonalAgentLimit,
    PersonalOwnerLeft,
    ensure_personal_capacity,
    get_or_create_personal_department,
    personal_frame,
)
from oc8.departments.repo import live_department
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SUBJECT = "ada"
#: Somebody else in the tenant: an administrator or a department manager who
#: holds no seat in Ada's personal department.
_OTHER = "root"
_IN_FRAME = {"mail_read": {"enabled": True, "read": True}}
_OUT_OF_FRAME = {"shell_exec": {"enabled": True, "read": True, "modify": True}}


def _headers(tenant: uuid.UUID, role: str = "member", subject: str = _SUBJECT) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _personal(
    app_session: AppSessionFactory, *, max_agents: int | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """A tenant with personal departments on, and the owner's personal department."""
    tenant = uuid.uuid4()
    settings: dict[str, Any] = {ENABLED_KEY: True, FRAME_KEY: _IN_FRAME}
    if max_agents is not None:
        settings[MAX_AGENTS_KEY] = max_agents
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:8]}",
                name="T",
                tier="standard",
                region="eu",
                settings=settings,
            )
        )
        member = m.OrgMember(
            tenant_id=tenant,
            subject=_SUBJECT,
            subject_uuid=subject_uuid_for(_SUBJECT),
            display_name="Ada",
        )
        db.add(member)
        await db.flush()
        dept = await get_or_create_personal_department(db, member=member)
        return tenant, dept.id


async def _standard(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant, name="Eng", frame={"tools": {**_IN_FRAME, **_OUT_OF_FRAME}}
        )
        db.add(dept)
        await db.flush()
        return dept.id


def _body(
    dept_id: uuid.UUID, name: str = "Mia", narrowing: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "departmentId": str(dept_id),
        "roleTitle": "Helper",
        "mission": "help",
    }
    if narrowing is not None:
        body["narrowing"] = {"tools": narrowing}
    return body


async def test_create_with_a_tool_outside_the_frame_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        r = await http.post(
            "/api/v1/agents",
            json=_body(dept_id, narrowing=_OUT_OF_FRAME),
            headers=_headers(tenant),
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "narrowing_exceeds_frame"


async def test_create_with_a_narrowing_inside_the_frame_works(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        r = await http.post(
            "/api/v1/agents",
            json=_body(dept_id, narrowing={"mail_read": {"enabled": True, "read": True}}),
            headers=_headers(tenant),
        )
    assert r.status_code == 201, r.text


async def test_put_narrowing_adding_a_key_outside_the_frame_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        made = await http.post("/api/v1/agents", json=_body(dept_id), headers=_headers(tenant))
        assert made.status_code == 201, made.text
        agent_id = made.json()["id"]
        r = await http.put(
            f"/api/v1/agents/{agent_id}/narrowing",
            json={"narrowing": {"tools": _OUT_OF_FRAME}},
            headers=_headers(tenant),
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "narrowing_exceeds_frame"
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, uuid.UUID(agent_id))
        assert agent is not None and "shell_exec" not in (agent.narrowing or {}).get("tools", {})


async def test_create_at_the_cap_is_a_409(app_session: AppSessionFactory) -> None:
    tenant, dept_id = await _personal(app_session, max_agents=1)
    async with _http() as http:
        first = await http.post("/api/v1/agents", json=_body(dept_id), headers=_headers(tenant))
        assert first.status_code == 201, first.text
        second = await http.post(
            "/api/v1/agents", json=_body(dept_id, "Max"), headers=_headers(tenant)
        )
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == {"error": "personal_agent_limit"}


async def test_restore_at_the_cap_is_a_409(app_session: AppSessionFactory) -> None:
    tenant, dept_id = await _personal(app_session, max_agents=1)
    async with _http() as http:
        first = await http.post("/api/v1/agents", json=_body(dept_id), headers=_headers(tenant))
        assert first.status_code == 201, first.text
        archived_id = uuid.UUID(first.json()["id"])
        async with app_session(tenant) as db:
            archived = await db.get(m.Agent, archived_id)
            assert archived is not None
            archived.deleted_at = datetime.now(UTC)
        second = await http.post(
            "/api/v1/agents", json=_body(dept_id, "Max"), headers=_headers(tenant)
        )
        assert second.status_code == 201, second.text
        r = await http.post(f"/api/v1/agents/{archived_id}/restore", headers=_headers(tenant))
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"error": "personal_agent_limit"}


async def test_restore_after_the_owner_left_is_refused(app_session: AppSessionFactory) -> None:
    """Nobody restores an agent of a departed owner: the owner's seat went with
    them, and an administrator never writes agents in a personal department
    (final review I2). The service's own `owner_left` refusal stays behind
    that as defence in depth (`tests/departments/test_offboarding_owned.py`)."""
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        made = await http.post("/api/v1/agents", json=_body(dept_id), headers=_headers(tenant))
        assert made.status_code == 201, made.text
        agent_id = uuid.UUID(made.json()["id"])
        async with app_session(tenant) as db:
            agent = await db.get(m.Agent, agent_id)
            dept = await db.get(m.Department, dept_id)
            assert agent is not None and dept is not None and dept.owner_member_id is not None
            agent.deleted_at = datetime.now(UTC)
        r = await http.post(f"/api/v1/agents/{agent_id}/restore", headers=_headers(tenant))
        assert r.status_code == 200, r.text  # control: the owner is still here
        async with app_session(tenant) as db:
            agent = await db.get(m.Agent, agent_id)
            dept = await db.get(m.Department, dept_id)
            assert agent is not None and dept is not None and dept.owner_member_id is not None
            owner = await db.get(m.OrgMember, dept.owner_member_id)
            assert owner is not None
            agent.deleted_at = datetime.now(UTC)
            owner.deleted_at = datetime.now(UTC)
        r = await http.post(
            f"/api/v1/agents/{agent_id}/restore",
            headers=_headers(tenant, "org_admin", _OTHER),
        )
    assert r.status_code == 404, r.text
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.deleted_at is not None


async def test_a_standard_department_keeps_its_behaviour(app_session: AppSessionFactory) -> None:
    """No personal frame, no personal cap: the same calls as above go through."""
    tenant, _ = await _personal(app_session, max_agents=1)
    std = await _standard(app_session, tenant)
    async with _http() as http:
        for name in ("A", "B"):
            r = await http.post(
                "/api/v1/agents",
                json=_body(std, name, narrowing=_OUT_OF_FRAME),
                headers=_headers(tenant, role="org_admin"),
            )
            assert r.status_code == 201, r.text
        agent_id = r.json()["id"]
        r = await http.put(
            f"/api/v1/agents/{agent_id}/narrowing",
            json={"narrowing": {"tools": {"mail_read": {"enabled": True, "read": True}}}},
            headers=_headers(tenant, role="org_admin"),
        )
        assert r.status_code == 200, r.text


async def test_two_concurrent_hires_at_the_cap_admit_exactly_one(
    app_session: AppSessionFactory,
) -> None:
    """Two transactions count at cap-1 and both insert: the department row lock
    makes the second wait for the first's commit and then see it. The first is
    still open (uncommitted agent) while the second runs into it."""
    tenant, dept_id = await _personal(app_session, max_agents=1)
    first_is_in = asyncio.Event()

    async def _hire(*, wait_for_first: bool) -> bool:
        if wait_for_first:
            await first_is_in.wait()
        async with app_session(tenant) as db:
            dept = await live_department(db, tenant_id=tenant, department_id=dept_id)
            assert dept is not None
            try:
                await ensure_personal_capacity(db, dept)
            except PersonalAgentLimit:
                return False
            db.add(
                m.Agent(
                    tenant_id=tenant,
                    department_id=dept_id,
                    name="x",
                    status="stopped",
                    narrowing={},
                    definition={},
                )
            )
            await db.flush()
            if not wait_for_first:
                first_is_in.set()
                await asyncio.sleep(0.3)
            return True

    results = await asyncio.wait_for(
        asyncio.gather(_hire(wait_for_first=False), _hire(wait_for_first=True)), timeout=30
    )
    assert sorted(results) == [False, True]


@pytest.mark.parametrize("first", ["hire", "offboarding"])
async def test_an_offboarding_racing_a_hire_leaves_no_live_agent(
    app_session: AppSessionFactory, first: str
) -> None:
    """Final review I1: the hire checked the owner, then locked; the sweep
    selected the agents without the department lock. A hire committing just
    after the sweep's select left a live agent with nobody to answer for it.
    Both now take the department row lock first: whichever goes second waits
    for the other's commit, and then either refuses the hire (owner gone) or
    archives the agent it now sees. The hire session holds the member in its
    identity map, as the route does (`_caller_member`)."""
    tenant, dept_id = await _personal(app_session)
    first_is_in = asyncio.Event()

    async def _hire() -> None:
        if first != "hire":
            await first_is_in.wait()
        async with app_session(tenant) as db:
            member = (
                await db.execute(select(m.OrgMember).where(m.OrgMember.subject == _SUBJECT))
            ).scalar_one()
            assert member.deleted_at is None
            dept = await live_department(db, tenant_id=tenant, department_id=dept_id)
            assert dept is not None
            try:
                await ensure_personal_capacity(db, dept)
            except PersonalOwnerLeft:
                return
            db.add(
                m.Agent(
                    tenant_id=tenant,
                    department_id=dept_id,
                    name="x",
                    status="stopped",
                    narrowing={},
                    definition={},
                )
            )
            await db.flush()
            if first == "hire":
                first_is_in.set()
                await asyncio.sleep(0.3)

    async def _offboard() -> None:
        if first != "offboarding":
            await first_is_in.wait()
        async with app_session(tenant) as db:
            member = (
                await db.execute(select(m.OrgMember).where(m.OrgMember.subject == _SUBJECT))
            ).scalar_one()
            member.deleted_at = datetime.now(UTC)
            await db.flush()
            await offboard_member(db, tenant_id=tenant, member_id=member.id)
            if first == "offboarding":
                first_is_in.set()
                await asyncio.sleep(0.3)

    await asyncio.wait_for(asyncio.gather(_hire(), _offboard()), timeout=30)
    async with app_session(tenant) as db:
        live = (
            (
                await db.execute(
                    select(m.Agent).where(
                        m.Agent.department_id == dept_id, m.Agent.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert live == []


async def test_concurrent_http_hires_at_the_cap_give_one_201_and_one_409(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session, max_agents=1)
    async with _http() as http:
        a, b = await asyncio.wait_for(
            asyncio.gather(
                http.post("/api/v1/agents", json=_body(dept_id, "A"), headers=_headers(tenant)),
                http.post("/api/v1/agents", json=_body(dept_id, "B"), headers=_headers(tenant)),
            ),
            timeout=60,
        )
    assert sorted([a.status_code, b.status_code]) == [201, 409], (a.text, b.text)


# --- Round 2: the PDP, restore clipping, rollback, admin instantiate ---


def test_a_personal_frame_is_closed_so_an_absent_key_grants_nothing() -> None:
    narrowing = {"tools": {"shell_exec": {"enabled": True, "read": True, "modify": True}}}
    closed = effective_tool_policies(personal_frame(_IN_FRAME), narrowing)
    assert "shell_exec" not in closed
    # The frame's own tool still resolves.
    assert closed["mail_read"].enabled is True


def test_a_standard_frame_still_allows_an_agent_exclusive_grant() -> None:
    narrowing = {"tools": {"shell_exec": {"enabled": True, "read": True}}}
    effective = effective_tool_policies({"tools": _IN_FRAME}, narrowing)
    assert effective["shell_exec"].enabled is True


async def _tighten_frame(
    app_session: AppSessionFactory, tenant: uuid.UUID, dept_id: uuid.UUID
) -> None:
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        dept.frame = personal_frame({})


async def _archive(app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        agent.deleted_at = datetime.now(UTC)


async def test_restore_clips_a_stale_narrowing_and_publishes_it(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        made = await http.post(
            "/api/v1/agents",
            json=_body(dept_id, narrowing=_IN_FRAME),
            headers=_headers(tenant),
        )
        assert made.status_code == 201, made.text
        agent_id = uuid.UUID(made.json()["id"])
        await _archive(app_session, tenant, agent_id)
        await _tighten_frame(app_session, tenant, dept_id)
        r = await http.post(f"/api/v1/agents/{agent_id}/restore", headers=_headers(tenant))
        assert r.status_code == 200, r.text
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.deleted_at is None
        assert (agent.narrowing or {}).get("tools") == {}
        versions = (
            (await db.execute(select(m.AgentVersion).where(m.AgentVersion.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(versions) == 2
        assert max(versions, key=lambda v: v.version_no).payload["narrowing"]["tools"] == {}


async def test_restore_joins_a_pending_draft_instead_of_publishing(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        made = await http.post(
            "/api/v1/agents",
            json=_body(dept_id, narrowing=_IN_FRAME),
            headers=_headers(tenant),
        )
        agent_id = uuid.UUID(made.json()["id"])
        renamed = await http.patch(
            f"/api/v1/agents/{agent_id}/instructions",
            json={"mission": "a draft"},
            headers=_headers(tenant),
        )
        assert renamed.status_code == 200, renamed.text
        await _archive(app_session, tenant, agent_id)
        await _tighten_frame(app_session, tenant, dept_id)
        r = await http.post(f"/api/v1/agents/{agent_id}/restore", headers=_headers(tenant))
        assert r.status_code == 200, r.text
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and (agent.narrowing or {}).get("tools") == {}
        versions = (
            (await db.execute(select(m.AgentVersion).where(m.AgentVersion.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(versions) == 1


async def test_rollback_to_a_version_outside_the_frame_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        made = await http.post(
            "/api/v1/agents",
            json=_body(dept_id, narrowing=_IN_FRAME),
            headers=_headers(tenant),
        )
        agent_id = made.json()["id"]
        await _tighten_frame(app_session, tenant, dept_id)
        # Ada herself, holding the admin role: a plain `member` lacks
        # `agent_version:publish`, which a rollback needs (see the report).
        r = await http.post(
            f"/api/v1/agents/{agent_id}/versions/1/rollback",
            json={},
            headers=_headers(tenant, role="org_admin"),
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "narrowing_exceeds_frame"}


async def test_the_admin_instantiate_route_refuses_a_personal_department(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session, max_agents=1)
    async with app_session(tenant) as db:
        db.add(
            m.Agent(
                tenant_id=tenant,
                department_id=dept_id,
                name="x",
                status="stopped",
                narrowing={},
                definition={},
            )
        )
        version = await install_plugin(
            db,
            tenant_id=tenant,
            manifest_data={
                "name": "mail_helper",
                "version": "1.0.0",
                "type": "agent_template",
                "trust": "first_party",
                "summary": "s",
                "agent_template": {"name": "Mia", "mission": "m", "scope": "personal"},
            },
        )
        await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
        capa_id = version.capa_id
    async with _http() as http:
        r = await http.post(
            f"/api/v1/capas/{capa_id}/instantiate",
            json={"departmentId": str(dept_id)},
            headers=_headers(tenant, "org_admin", _OTHER),
        )
    # Final review I2: only the owner's own hire route reaches a personal
    # department -- not even at the cap does the admin route get that far.
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "department not found"


async def _owner_left(
    app_session: AppSessionFactory, tenant: uuid.UUID, dept_id: uuid.UUID
) -> None:
    """The owner leaves the way the members route does it: offboarding, then the
    soft delete."""
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None and dept.owner_member_id is not None
        owner = await db.get(m.OrgMember, dept.owner_member_id)
        assert owner is not None
        await offboard_member(db, tenant_id=tenant, member_id=owner.id)
        owner.deleted_at = datetime.now(UTC)


async def test_creating_an_agent_in_a_departed_owners_department_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    await _owner_left(app_session, tenant, dept_id)
    async with _http() as http:
        r = await http.post(
            "/api/v1/agents", json=_body(dept_id), headers=_headers(tenant, "org_admin", _OTHER)
        )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "unknown department"


async def test_a_template_hire_into_a_departed_owners_department_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with app_session(tenant) as db:
        version = await install_plugin(
            db,
            tenant_id=tenant,
            manifest_data={
                "name": "mail_helper",
                "version": "1.0.0",
                "type": "agent_template",
                "trust": "first_party",
                "summary": "s",
                "agent_template": {"name": "Mia", "mission": "m", "scope": "personal"},
            },
        )
        await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
        version_id = version.id
    await _owner_left(app_session, tenant, dept_id)
    # The owner's own hire is the only path into the department (final review
    # I2), and a departed owner can no longer call it; the service refuses it
    # all the same.
    async with app_session(tenant) as db:
        enabled = await db.get(m.CapaVersion, version_id)
        assert enabled is not None
        with pytest.raises(PersonalOwnerLeft):
            await instantiate_agent(
                db,
                tenant_id=tenant,
                version=enabled,
                department_id=dept_id,
                hire_gate=False,
                owner_hire=True,
            )


async def test_restoring_a_personal_department_whose_owner_left_is_a_409(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _personal(app_session)
    await _owner_left(app_session, tenant, dept_id)
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        dept.deleted_at = datetime.now(UTC)
    async with _http() as http:
        r = await http.post(
            f"/api/v1/departments/{dept_id}/restore",
            headers=_headers(tenant, "org_admin", _OTHER),
        )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"error": "owner_left"}
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None and dept.deleted_at is not None


# --- Final review I2: agent writes in a personal department are the owner's ---


async def _owners_agent(
    app_session: AppSessionFactory,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ada's personal department with one agent she made, and its v1."""
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        made = await http.post(
            "/api/v1/agents", json=_body(dept_id, narrowing=_IN_FRAME), headers=_headers(tenant)
        )
        assert made.status_code == 201, made.text
    return tenant, dept_id, uuid.UUID(made.json()["id"])


@pytest.mark.parametrize("role", ["org_admin", "dept_manager"])
async def test_somebody_else_cannot_create_an_agent_in_a_personal_department(
    app_session: AppSessionFactory, role: str
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with _http() as http:
        r = await http.post(
            "/api/v1/agents", json=_body(dept_id), headers=_headers(tenant, role, _OTHER)
        )
    # The create route's own not-found answer: the same as for an unknown id.
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "unknown department"
    async with app_session(tenant) as db:
        rows = await db.execute(select(m.Agent).where(m.Agent.department_id == dept_id))
        assert rows.scalars().all() == []


@pytest.mark.parametrize("role", ["org_admin", "dept_manager"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "instructions", {"instructions": "do something else"}),
        ("POST", "lifecycle", {"action": "start"}),
        ("POST", "versions/1/rollback", {}),
    ],
    ids=["instructions", "lifecycle start", "rollback"],
)
async def test_somebody_else_cannot_write_a_personal_agent(
    app_session: AppSessionFactory, role: str, method: str, path: str, body: dict[str, Any]
) -> None:
    tenant, _dept_id, agent_id = await _owners_agent(app_session)
    async with app_session(tenant) as db:
        before = await db.get(m.Agent, agent_id)
        assert before is not None
        status_before, instructions_before = before.status, before.mission
    async with _http() as http:
        r = await http.request(
            method,
            f"/api/v1/agents/{agent_id}/{path}",
            json=body,
            headers=_headers(tenant, role, _OTHER),
        )
    assert r.status_code in (403, 404), r.text
    if role == "org_admin":
        # Holds every door permission: refused at the department, as not found.
        assert r.status_code == 404, r.text
    async with app_session(tenant) as db:
        after = await db.get(m.Agent, agent_id)
        assert after is not None
        assert (after.status, after.mission) == (status_before, instructions_before)
        versions = await db.execute(
            select(m.AgentVersion).where(m.AgentVersion.agent_id == agent_id)
        )
        assert len(versions.scalars().all()) == 1


async def test_the_owner_still_writes_her_own_agent(app_session: AppSessionFactory) -> None:
    tenant, _dept_id, agent_id = await _owners_agent(app_session)
    async with _http() as http:
        r = await http.patch(
            f"/api/v1/agents/{agent_id}/instructions",
            json={"instructions": "mine"},
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text
        r = await http.post(
            f"/api/v1/agents/{agent_id}/lifecycle",
            json={"action": "start"},
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text


@pytest.mark.parametrize("role", ["org_admin", "dept_manager"])
async def test_somebody_else_cannot_instantiate_into_a_personal_department(
    app_session: AppSessionFactory, role: str
) -> None:
    tenant, dept_id = await _personal(app_session)
    async with app_session(tenant) as db:
        version = await install_plugin(
            db,
            tenant_id=tenant,
            manifest_data={
                "name": "mail_helper",
                "version": "1.0.0",
                "type": "agent_template",
                "trust": "first_party",
                "summary": "s",
                "agent_template": {"name": "Mia", "mission": "m", "scope": "personal"},
            },
        )
        await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
        capa_id = version.capa_id
    async with _http() as http:
        r = await http.post(
            f"/api/v1/capas/{capa_id}/instantiate",
            json={"departmentId": str(dept_id)},
            headers=_headers(tenant, role, _OTHER),
        )
    assert r.status_code in (400, 403), r.text
    async with app_session(tenant) as db:
        rows = await db.execute(select(m.Agent).where(m.Agent.department_id == dept_id))
        assert rows.scalars().all() == []


async def test_an_administrator_cannot_touch_a_personal_agents_triggers_or_skills(
    app_session: AppSessionFactory,
) -> None:
    """The routes that write an agent behind a plain permission, outside
    `authorize_agent_write`, keep to the same owner-only rule."""
    tenant, _dept_id, agent_id = await _owners_agent(app_session)
    async with app_session(tenant) as db:
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            task_text="t",
            cron_expression="0 9 * * *",
            enabled=False,
        )
        db.add(trigger)
        await db.flush()
        trigger_id = trigger.id
    admin = _headers(tenant, "org_admin", _OTHER)
    async with _http() as http:
        made = await http.post(
            f"/api/v1/agents/{agent_id}/triggers",
            json={"kind": "cron", "taskText": "go", "cronExpression": "0 9 * * *"},
            headers=admin,
        )
        assert made.status_code == 404, made.text
        patched = await http.patch(
            f"/api/v1/triggers/{trigger_id}", json={"taskText": "other"}, headers=admin
        )
        assert patched.status_code == 404, patched.text
        deleted = await http.delete(f"/api/v1/triggers/{trigger_id}", headers=admin)
        assert deleted.status_code == 404, deleted.text
        unassigned = await http.delete(
            f"/api/v1/agents/{agent_id}/skills/{uuid.uuid4()}", headers=admin
        )
        assert unassigned.status_code == 404, unassigned.text
        assert unassigned.json()["detail"] == "agent not found"
    async with app_session(tenant) as db:
        kept = await db.get(m.Trigger, trigger_id)
        assert kept is not None and kept.task_text == "t"
        rows = await db.execute(select(m.Trigger).where(m.Trigger.agent_id == agent_id))
        assert len(rows.scalars().all()) == 1


async def test_restoring_an_old_personal_department_beside_a_live_one_is_a_409(
    app_session: AppSessionFactory,
) -> None:
    """Final review M1: the owner has had a new personal department since the
    old one was archived. Restoring the old one would break the one-live-
    personal-department-per-owner index (a 500); it is a 409 instead."""
    tenant, old_id = await _personal(app_session)
    async with app_session(tenant) as db:
        old = await db.get(m.Department, old_id)
        assert old is not None and old.owner_member_id is not None
        old.deleted_at = datetime.now(UTC)
        await db.flush()
        owner = await db.get(m.OrgMember, old.owner_member_id)
        assert owner is not None
        new = await get_or_create_personal_department(db, member=owner)
        assert new.id != old_id
    async with _http() as http:
        r = await http.post(
            f"/api/v1/departments/{old_id}/restore",
            headers=_headers(tenant, "org_admin", _OTHER),
        )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"error": "personal_department_exists"}
    async with app_session(tenant) as db:
        old = await db.get(m.Department, old_id)
        assert old is not None and old.deleted_at is not None
