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

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.scope import subject_uuid_for
from oc8.departments.personal import (
    ENABLED_KEY,
    FRAME_KEY,
    MAX_AGENTS_KEY,
    PersonalAgentLimit,
    ensure_personal_capacity,
    get_or_create_personal_department,
)
from oc8.departments.repo import live_department
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SUBJECT = "ada"
_IN_FRAME = {"mail_read": {"enabled": True, "read": True}}
_OUT_OF_FRAME = {"shell_exec": {"enabled": True, "read": True, "modify": True}}


def _headers(tenant: uuid.UUID, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=_SUBJECT, role=role)
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
