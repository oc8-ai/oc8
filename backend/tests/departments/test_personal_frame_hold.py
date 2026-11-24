"""A department with an owner takes an administrator's tightening of its frame
tools at once and holds a loosening in `pending_frame_tools` until the owner
confirms it, so the owner's confirmation of a login binding keeps holding
(`departments.personal.apply_held_tools`). A department without live agents,
and a standard department, take every change directly."""

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
from oc8.authz.pdp import ToolPolicy
from oc8.connections.access import unconfirmed_reason
from oc8.main import create_app
from tests.conftest import AppSessionFactory
from tests.departments.owned import OwnedWorld, owned_world
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio

_RW: dict[str, Any] = {"enabled": True, "read": True, "modify": True}
_R: dict[str, Any] = {"enabled": True, "read": True, "modify": False}


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="admin", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _policy(raw: dict[str, Any]) -> ToolPolicy:
    return ToolPolicy.from_json(dict(raw))


def _same_tools(got: dict[str, Any] | None, want: dict[str, Any]) -> bool:
    """Equal as the runtime reads them: the stored shape may differ (a request
    stores every field, a meet its own `to_json`)."""
    return (
        got is not None
        and set(got) == set(want)
        and all(_policy(got[k]) == _policy(want[k]) for k in want)
    )


async def _dept(
    app_session: AppSessionFactory, tenant: uuid.UUID, dept_id: uuid.UUID
) -> m.Department:
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        return dept


async def _reason(app_session: AppSessionFactory, w: OwnedWorld, tool_key: str) -> str | None:
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        row = await db.scalar(
            select(m.AgentToolAccess).where(
                m.AgentToolAccess.agent_id == w.agent_id, m.AgentToolAccess.tool_key == tool_key
            )
        )
        assert agent is not None and row is not None and w.credential_id is not None
        return await unconfirmed_reason(
            db,
            row,
            agent,
            tool_key,
            w.credential_id,
            w.member_id,
            version_id=agent.current_version_id,
        )


async def _put_tools(
    tenant: uuid.UUID, dept_id: uuid.UUID, tools: dict[str, Any]
) -> dict[str, Any]:
    async with _http() as c:
        r = await c.put(
            f"/api/v1/departments/{dept_id}/tools", json={"tools": tools}, headers=_headers(tenant)
        )
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body


async def test_a_mixed_save_applies_the_tightening_and_holds_the_loosening(
    app_session: AppSessionFactory,
) -> None:
    w = await owned_world(app_session, tools={"a": _RW, "b": _R}, tool_key="a")
    assert await _reason(app_session, w, "a") is None

    async with _http() as c:
        r = await c.put(
            "/api/v1/personal-departments/default-frame",
            json={"tools": {"a": _R, "b": _RW}},
            headers=_headers(w.tenant),
        )
        assert r.status_code == 200, r.text

    dept = await _dept(app_session, w.tenant, w.department_id)
    assert _same_tools(dept.frame["tools"], {"a": _R, "b": _R})
    assert _same_tools(dept.pending_frame_tools, {"a": _R, "b": _RW})
    assert dept.frame["closed"] is True
    assert await _reason(app_session, w, "a") is None


async def test_the_admin_override_holds_a_loosening_too(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _R}, tool_key="a")

    body = await _put_tools(w.tenant, w.department_id, {"a": _RW})

    dept = await _dept(app_session, w.tenant, w.department_id)
    assert _same_tools(dept.frame["tools"], {"a": _R})
    assert _same_tools(dept.pending_frame_tools, {"a": _RW})
    assert dept.frame_overridden_at is not None
    # The answer shows what applies now, not what waits.
    assert _same_tools(body["tools"], {"a": _R})
    assert await _reason(app_session, w, "a") is None

    # Undoing the loosening leaves nothing to wait for.
    await _put_tools(w.tenant, w.department_id, {"a": _R})
    dept = await _dept(app_session, w.tenant, w.department_id)
    assert _same_tools(dept.frame["tools"], {"a": _R})
    assert dept.pending_frame_tools is None


async def test_a_reset_that_loosens_is_held(app_session: AppSessionFactory) -> None:
    w = await owned_world(
        app_session, tools={"a": _R}, tool_key="a", frame_capa_defaults={"tools": {"a": _RW}}
    )

    async with _http() as c:
        r = await c.post(
            f"/api/v1/departments/{w.department_id}/tools/a/reset", headers=_headers(w.tenant)
        )
        assert r.status_code == 200, r.text

    dept = await _dept(app_session, w.tenant, w.department_id)
    assert _same_tools(dept.frame["tools"], {"a": _R})
    assert _same_tools(dept.pending_frame_tools, {"a": _RW})
    assert await _reason(app_session, w, "a") is None


async def test_a_malformed_reset_is_refused_before_it_is_held(
    app_session: AppSessionFactory,
) -> None:
    w = await owned_world(app_session, tools={"a": _R}, tool_key="a")
    malformed = {**_RW, "limits": [{"tools": [], "max": 1, "per": "day"}]}
    async with app_session(w.tenant) as db:
        dept = await db.get(m.Department, w.department_id)
        assert dept is not None
        dept.frame_capa_defaults = {"tools": {"a": malformed}}

    async with _http() as c:
        r = await c.post(
            f"/api/v1/departments/{w.department_id}/tools/a/reset", headers=_headers(w.tenant)
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "frame_tools_malformed"

    dept = await _dept(app_session, w.tenant, w.department_id)
    assert dept.frame["tools"] == {"a": _R}
    assert dept.pending_frame_tools is None


async def test_a_held_meet_keeps_the_fields_the_policy_does_not_read(
    app_session: AppSessionFactory,
) -> None:
    login = str(uuid.uuid4())
    w = await owned_world(app_session, tools={"a": {**_R, "default_connection_id": login}})

    await _put_tools(w.tenant, w.department_id, {"a": {**_RW, "defaultConnectionId": login}})

    dept = await _dept(app_session, w.tenant, w.department_id)
    assert _same_tools(dept.frame["tools"], {"a": _R})
    assert dept.frame["tools"]["a"]["default_connection_id"] == login
    assert _same_tools(dept.pending_frame_tools, {"a": _RW})


async def test_a_change_the_policy_does_not_read_applies_and_breaks_the_confirmation(
    app_session: AppSessionFactory,
) -> None:
    """T3-3: `default_connection_id` is no part of `ToolPolicy`, so the hold
    does not see it. It applies at once, and the owner's confirmation fails
    closed instead of holding a change it never saw."""
    w = await owned_world(app_session, tools={"a": _R}, tool_key="a")
    assert await _reason(app_session, w, "a") is None
    login = str(uuid.uuid4())

    await _put_tools(w.tenant, w.department_id, {"a": {**_R, "defaultConnectionId": login}})

    dept = await _dept(app_session, w.tenant, w.department_id)
    assert dept.frame["tools"]["a"]["default_connection_id"] == login
    assert dept.pending_frame_tools is None
    assert await _reason(app_session, w, "a") == "agent_changed"


async def test_a_department_without_live_agents_takes_the_change_directly(
    app_session: AppSessionFactory,
) -> None:
    w = await owned_world(app_session, tools={"a": _R})
    await _put_tools(w.tenant, w.department_id, {"a": _RW, "b": _RW})
    dept = await _dept(app_session, w.tenant, w.department_id)
    assert dept.pending_frame_tools is not None  # held while the agent lives

    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        agent.deleted_at = agent.created_at
    await _put_tools(w.tenant, w.department_id, {"a": _RW})

    dept = await _dept(app_session, w.tenant, w.department_id)
    assert _same_tools(dept.frame["tools"], {"a": _RW})
    # The earlier held loosening is gone with it: the newest write is what counts.
    assert dept.pending_frame_tools is None


async def test_a_standard_department_is_never_held(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = {"tools": {"a": _R}}
        dept.frame_capa_defaults = {"tools": {"b": _RW}}

    await _put_tools(agent.tenant_id, agent.department_id, {"a": _RW, "b": _R})
    dept = await _dept(app_session, agent.tenant_id, agent.department_id)
    assert _same_tools(dept.frame["tools"], {"a": _RW, "b": _R})
    assert dept.pending_frame_tools is None

    async with _http() as c:
        r = await c.post(
            f"/api/v1/departments/{agent.department_id}/tools/b/reset",
            headers=_headers(agent.tenant_id),
        )
        assert r.status_code == 200, r.text
    dept = await _dept(app_session, agent.tenant_id, agent.department_id)
    assert _same_tools(dept.frame["tools"], {"a": _RW, "b": _RW})
    assert dept.pending_frame_tools is None


async def test_the_admin_list_flags_a_waiting_department(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _R})

    async def flagged() -> bool:
        async with _http() as c:
            r = await c.get("/api/v1/personal-departments", headers=_headers(w.tenant))
            assert r.status_code == 200, r.text
            (row,) = [d for d in r.json() if d["id"] == str(w.department_id)]
            return bool(row["pendingConfirmation"])

    assert await flagged() is False
    await _put_tools(w.tenant, w.department_id, {"a": _RW})
    assert await flagged() is True


async def test_contracts_are_refused_on_a_department_with_an_owner(
    app_session: AppSessionFactory,
) -> None:
    """F3: `emits`/`intakes` are part of the frame the owner confirms, and the
    hold covers only the tools, so the route refuses instead of applying."""
    w = await owned_world(app_session, tools={"a": _R})
    before = (await _dept(app_session, w.tenant, w.department_id)).frame
    agent = await _make_agent(app_session)

    async with _http() as c:
        r = await c.put(
            f"/api/v1/departments/{w.department_id}/contracts",
            json={"emits": ["x.done"], "intakes": []},
            headers=_headers(w.tenant),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "frame_owned_by_member"

        standard = await c.put(
            f"/api/v1/departments/{agent.department_id}/contracts",
            json={"emits": ["x.done"], "intakes": []},
            headers=_headers(agent.tenant_id),
        )
        assert standard.status_code == 200, standard.text

    assert (await _dept(app_session, w.tenant, w.department_id)).frame == before
    assert (await _dept(app_session, agent.tenant_id, agent.department_id)).frame["emits"] == [
        "x.done"
    ]
