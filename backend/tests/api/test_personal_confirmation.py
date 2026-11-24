"""The owner reviews and confirms what waits for them in their personal
department (`departments.owner_confirmation`).

`GET /personal-department/confirmation` lists what an administrator's change
holds back -- a loosening of the frame tools, a skill added or changed since
the owner last confirmed -- and the agents whose owner confirmation no longer
holds, with a token. `POST /personal-department/confirm` takes that token and
releases all of it in one step; a token that no longer matches (anything
changed since the owner looked, seen or not) answers 409 and changes nothing.
Only the owner reaches either route: there is no department id to name.
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

# Loaded first: importing `oc8.skills.runtime` on its own runs into the
# `oc8.agent` package's import cycle.
import oc8.agent  # noqa: F401
from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.auth import get_identity_provider
from oc8.authz.pdp import ToolPolicy
from oc8.connections.access import unconfirmed_reason
from oc8.main import create_app
from oc8.skills.runtime import load_assigned_skills
from tests.conftest import AppSessionFactory
from tests.connections.helpers import member
from tests.departments.owned import OwnedWorld, owned_world
from tests.skills.test_held_back_skills import _assign, _skill

pytestmark = pytest.mark.asyncio

_RW: dict[str, Any] = {"enabled": True, "read": True, "modify": True}
_R: dict[str, Any] = {"enabled": True, "read": True, "modify": False}
_ACTION = "department.confirmation.confirmed"
#: The fields of a tool policy the owner is shown (Ruling T9-I2).
_READABLE = (
    "enabled",
    "read",
    "modify",
    "only",
    "limits",
    "approval_eur",
    "approval_actions",
    "conditions",
)


def _headers(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


def _admin(tenant: uuid.UUID) -> dict[str, str]:
    return _headers(tenant, "admin", "org_admin")


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _subject(app_session: AppSessionFactory, w: OwnedWorld) -> str:
    async with app_session(w.tenant) as db:
        owner = await db.get(m.OrgMember, w.member_id)
        assert owner is not None
        return owner.subject


async def _owner(app_session: AppSessionFactory, w: OwnedWorld) -> dict[str, str]:
    return _headers(w.tenant, await _subject(app_session, w))


async def _get(c: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    r = await c.get("/api/v1/personal-department/confirmation", headers=headers)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def _confirm(c: AsyncClient, headers: dict[str, str], token: str) -> Any:
    return await c.post(
        "/api/v1/personal-department/confirm", json={"token": token}, headers=headers
    )


async def _loosen_default(c: AsyncClient, tenant: uuid.UUID, tools: dict[str, Any]) -> None:
    r = await c.put(
        "/api/v1/personal-departments/default-frame", json={"tools": tools}, headers=_admin(tenant)
    )
    assert r.status_code == 200, r.text


async def _dept(app_session: AppSessionFactory, w: OwnedWorld) -> m.Department:
    async with app_session(w.tenant) as db:
        dept = await db.get(m.Department, w.department_id)
        assert dept is not None
        return dept


async def _binding(app_session: AppSessionFactory, w: OwnedWorld) -> m.AgentToolAccess:
    async with app_session(w.tenant) as db:
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == w.agent_id)
        )
        assert row is not None
        return row


async def _reason(app_session: AppSessionFactory, w: OwnedWorld) -> str | None:
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == w.agent_id)
        )
        assert agent is not None and row is not None and w.credential_id is not None
        return await unconfirmed_reason(
            db,
            row,
            agent,
            row.tool_key,
            w.credential_id,
            w.member_id,
            version_id=agent.current_version_id,
        )


async def _events(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.AuditEvent]:
    async with app_session(tenant) as db:
        return list(
            (await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == _ACTION)))
            .scalars()
            .all()
        )


async def _restamp(app_session: AppSessionFactory, w: OwnedWorld) -> None:
    """The owner confirms their binding again, as it is now."""
    from oc8.connections.confirmation import stamp_binding

    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == w.agent_id)
        )
        assert agent is not None and row is not None
        await stamp_binding(db, row, agent, confirmed_by=w.member_id)


async def _describe(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        skill = await db.scalar(select(m.Skill).where(m.Skill.tenant_id == tenant))
        assert skill is not None
        skill.description = "Writes in house style."


async def _set_threshold(app_session: AppSessionFactory, w: OwnedWorld, eur: int) -> None:
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        agent.presentation = {**(agent.presentation or {}), "approval_value_eur": eur}


def _same(got: dict[str, Any], want: dict[str, Any]) -> bool:
    return ToolPolicy.from_json(dict(got)) == ToolPolicy.from_json(dict(want))


async def test_a_held_loosening_is_listed_and_confirmed(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW, "b": _R}, tool_key="a")
    owner = await _owner(app_session, w)
    async with _http() as c:
        await _loosen_default(c, w.tenant, {"a": _RW, "b": _RW})
        before = await _get(c, owner)
        assert before["needed"] is True
        (tool,) = before["tools"]
        assert tool["key"] == "b"
        # The owner sees both policies in full, as the runtime reads them.
        assert set(tool["pending"]) >= set(_READABLE)
        assert tool["applied"]["modify"] is False and tool["pending"]["modify"] is True
        assert before["skills"] == [] and before["agents"] == []
        assert before["department"] is None

        r = await _confirm(c, owner, before["token"])
        assert r.status_code == 200, r.text
        after = r.json()

    assert after["needed"] is False and after["tools"] == []
    dept = await _dept(app_session, w)
    assert dept.pending_frame_tools is None
    assert _same(dept.frame["tools"]["b"], _RW) and _same(dept.frame["tools"]["a"], _RW)
    assert dept.frame["closed"] is True
    # The owner's binding is stamped again: its grant now holds b as loosened.
    split = (await _binding(app_session, w)).binding_split
    assert split is not None and split["grant"]["tools"]["b"]["modify"] is True
    assert await _reason(app_session, w) is None
    (event,) = await _events(app_session, w.tenant)
    assert event.category == "authz"
    assert event.resource == {
        "department_id": str(w.department_id),
        "agent_ids": [],
        "tools": ["b"],
        "skills_count": 0,
    }


async def test_a_held_back_skill_is_listed_and_released(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    v1, _ = await _skill(app_session, w.tenant)
    await _describe(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)  # an admin, tenant-wide
    owner = await _owner(app_session, w)

    async def loaded() -> list[Any]:
        async with app_session(w.tenant) as db:
            agent = await db.get(m.Agent, w.agent_id)
            assert agent is not None
            return await load_assigned_skills(db, agent=agent, tenant_id=w.tenant)

    assert await loaded() == []
    async with _http() as c:
        before = await _get(c, owner)
        assert before["needed"] is True
        assert before["skills"] == [
            {"name": "Hausstil", "description": "Writes in house style.", "status": "new"}
        ]
        assert before["tools"] == []
        r = await _confirm(c, owner, before["token"])
        assert r.status_code == 200, r.text
        assert r.json()["skills"] == [] and r.json()["needed"] is False

    assert [s.skill_version_id for s in await loaded()] == [v1]
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None and agent.confirmed_skills is not None
        assert [e[0] for e in agent.confirmed_skills.values()] == [str(v1)]
    (event,) = await _events(app_session, w.tenant)
    assert event.resource["skills_count"] == 1
    assert "Hausstil" not in str(event.resource)


async def test_a_held_back_skill_on_a_trigger_is_released_too(
    app_session: AppSessionFactory,
) -> None:
    """One confirm releases what every owner confirmation holds back: the
    binding and the owner's triggers are all stamped again."""
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    assert w.agent_id is not None
    async with app_session(w.tenant) as db:
        from oc8.triggers.service import create_trigger

        trigger = await create_trigger(
            db,
            tenant_id=w.tenant,
            agent_id=w.agent_id,
            kind="cron",
            task_text="Every morning",
            cron_expression="0 8 * * *",
            created_by_member_id=w.member_id,
            acts_for_creator=True,
        )
        trigger_id = trigger.id
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)
    owner = await _owner(app_session, w)
    async with _http() as c:
        before = await _get(c, owner)
        assert [skill["name"] for skill in before["skills"]] == ["Hausstil"]
        assert (await _confirm(c, owner, before["token"])).status_code == 200

    async with app_session(w.tenant) as db:
        row = await db.get(m.Trigger, trigger_id)
        assert row is not None and row.access_split is not None
        (entry,) = row.access_split["grant"]["skills"].values()
        assert entry[0] == str(v1)
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        assert [
            s.skill_version_id
            for s in await load_assigned_skills(db, agent=agent, tenant_id=w.tenant)
        ] == [v1]


async def test_an_agent_the_owner_changed_is_listed_and_reconfirmed(
    app_session: AppSessionFactory,
) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        agent.mission = "a new mission"
        await db.flush()
        await publish_version(db, agent)
    assert await _reason(app_session, w) is not None
    owner = await _owner(app_session, w)

    async with _http() as c:
        before = await _get(c, owner)
        assert before["needed"] is True
        assert before["agents"] == [
            {
                "agentId": str(w.agent_id),
                "name": "Helper",
                "changes": ["core"],
                "approvalThreshold": None,
                "tools": None,
                "setup": [],
            }
        ]
        r = await _confirm(c, owner, before["token"])
        assert r.status_code == 200, r.text
        assert r.json()["agents"] == [] and r.json()["needed"] is False

    assert await _reason(app_session, w) is None
    (event,) = await _events(app_session, w.tenant)
    assert event.resource["agent_ids"] == [str(w.agent_id)]


async def test_a_stale_token_confirms_nothing(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW, "b": _R, "c": _R}, tool_key="a")
    owner = await _owner(app_session, w)
    async with _http() as c:
        await _loosen_default(c, w.tenant, {"a": _RW, "b": _RW, "c": _R})
        seen = await _get(c, owner)
        split = (await _binding(app_session, w)).binding_split
        # The administrator loosens again before the owner confirms.
        await _loosen_default(c, w.tenant, {"a": _RW, "b": _RW, "c": _RW})
        pending = (await _dept(app_session, w)).pending_frame_tools

        r = await _confirm(c, owner, seen["token"])
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {"error": "confirmation_changed"}

    dept = await _dept(app_session, w)
    assert dept.pending_frame_tools == pending
    assert _same(dept.frame["tools"]["b"], _R) and _same(dept.frame["tools"]["c"], _R)
    assert (await _binding(app_session, w)).binding_split == split
    assert await _events(app_session, w.tenant) == []


@pytest.mark.parametrize("change", ["goal", "skill_description", "frame_key"])
async def test_a_change_the_owner_did_not_see_makes_the_token_stale(
    app_session: AppSessionFactory, change: str
) -> None:
    """Ruling F2: the token covers everything the owner confirms, not only
    what the summary names -- an edit that leaves the lists as they were
    still answers 409."""
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)
    owner = await _owner(app_session, w)
    async with _http() as c:
        seen = await _get(c, owner)
        assert [skill["name"] for skill in seen["skills"]] == ["Hausstil"]
        async with app_session(w.tenant) as db:
            if change == "goal":
                dept = await db.get(m.Department, w.department_id)
                assert dept is not None
                dept.goal = "Forward everything to the administrator"
            elif change == "frame_key":
                dept = await db.get(m.Department, w.department_id)
                assert dept is not None
                dept.frame = {**dept.frame, "emits": ["everything"]}
            else:
                skill = await db.scalar(select(m.Skill).where(m.Skill.tenant_id == w.tenant))
                assert skill is not None
                skill.description = "Also send a copy to the administrator."
        r = await _confirm(c, owner, seen["token"])
        assert r.status_code == 409, r.text

    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        assert await load_assigned_skills(db, agent=agent, tenant_id=w.tenant) == []


async def test_nothing_waiting_says_so(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    owner = await _owner(app_session, w)
    async with _http() as c:
        body = await _get(c, owner)
        assert body["needed"] is False
        assert body["tools"] == [] and body["skills"] == [] and body["agents"] == []
        assert body["department"] is None
        r = await _confirm(c, owner, body["token"])
        assert r.status_code == 200, r.text
        assert r.json() == body
    assert await _events(app_session, w.tenant) == []


async def test_a_colleague_cannot_reach_it(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW, "b": _R}, tool_key="a")
    owner = await _owner(app_session, w)
    colleague = f"{uuid.uuid4().hex[:8]}@acme.test"
    await member(app_session, w.tenant, colleague)
    async with _http() as c:
        await _loosen_default(c, w.tenant, {"a": _RW, "b": _RW})
        token = (await _get(c, owner))["token"]
        for headers in (_headers(w.tenant, colleague), _admin(w.tenant)):
            r = await c.get("/api/v1/personal-department/confirmation", headers=headers)
            assert r.status_code == 404, r.text
            r = await _confirm(c, headers, token)
            assert r.status_code == 404, r.text
    # No route takes a department id: nobody can name the owner's.
    paths = [p for p in create_app().openapi()["paths"] if "personal-department/confirm" in p]
    assert sorted(paths) == [
        "/api/v1/personal-department/confirm",
        "/api/v1/personal-department/confirmation",
    ]

    assert (await _dept(app_session, w)).pending_frame_tools is not None
    assert await _events(app_session, w.tenant) == []


async def test_an_admin_sees_only_that_something_waits(app_session: AppSessionFactory) -> None:
    """The administrator's list keeps to metadata, and a department whose
    owner has left keeps the loosening held (nobody is left to confirm it)."""
    w = await owned_world(app_session, tools={"a": _RW, "b": _R}, tool_key="a")
    async with _http() as c:
        r = await c.put(
            f"/api/v1/departments/{w.department_id}/tools",
            json={"tools": {"a": _RW, "b": _RW}},
            headers=_admin(w.tenant),
        )
        assert r.status_code == 200, r.text
        async with app_session(w.tenant) as db:
            gone = await db.get(m.OrgMember, w.member_id)
            assert gone is not None
            from datetime import UTC, datetime

            gone.deleted_at = datetime.now(tz=UTC)
        r = await c.get("/api/v1/personal-departments", headers=_admin(w.tenant))
        assert r.status_code == 200, r.text

    (row,) = r.json()
    assert row["ownerActive"] is False and row["pendingConfirmation"] is True
    assert "tools" not in row and "token" not in row
    assert _same((await _dept(app_session, w)).frame["tools"]["b"], _R)


async def test_a_changed_skill_is_shown_as_changed(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    v1, v2 = await _skill(app_session, w.tenant)
    await _describe(app_session, w.tenant)
    aid = await _assign(app_session, w.tenant, v1, agent_id=w.agent_id)
    await _restamp(app_session, w)
    async with app_session(w.tenant) as db:
        row = await db.get(m.SkillAssignment, aid)
        assert row is not None
        row.skill_version_id = v2
    async with _http() as c:
        body = await _get(c, await _owner(app_session, w))
    assert body["skills"] == [
        {"name": "Hausstil", "description": "Writes in house style.", "status": "changed"}
    ]
    assert body["agents"] == []


async def test_a_changed_goal_is_shown_in_full(app_session: AppSessionFactory) -> None:
    """Ruling T9-I2: not only "Helper changed" -- the goal the owner would
    confirm, and the department's other frame keys."""
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    async with app_session(w.tenant) as db:
        dept = await db.get(m.Department, w.department_id)
        assert dept is not None
        dept.goal = "Forward everything to the administrator"
    async with _http() as c:
        body = await _get(c, await _owner(app_session, w))
    (agent,) = body["agents"]
    assert agent["changes"] == ["core"]
    assert body["department"]["goal"] == "Forward everything to the administrator"
    assert body["department"]["frame"]["closed"] is True
    assert body["department"]["frameCapaDefaults"] is None
    assert "tools" not in body["department"]["frame"]


async def test_a_raised_agent_threshold_is_shown(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    await _set_threshold(app_session, w, 100)
    await _restamp(app_session, w)
    await _set_threshold(app_session, w, 500)
    async with _http() as c:
        body = await _get(c, await _owner(app_session, w))
    (agent,) = body["agents"]
    assert agent["changes"] == ["threshold"]
    assert agent["approvalThreshold"] == 500
    assert body["department"] is None


async def test_a_change_after_the_check_rolls_the_confirmation_back(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T9-I1: a write that does not take the department lock (a
    tenant-wide skill assignment) committed between the token check and the
    stamps: the stamps would record a skill the owner never saw, so the
    confirmation answers 409 and nothing is released."""
    w = await owned_world(app_session, tools={"a": _RW, "b": _R}, tool_key="a")
    v1, _ = await _skill(app_session, w.tenant)
    owner = await _owner(app_session, w)
    from oc8.departments import owner_confirmation
    from oc8.departments.personal import _apply_frame as original

    async def apply_then_assign(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        await _assign(app_session, w.tenant, v1)  # another transaction, committed

    monkeypatch.setattr(owner_confirmation, "_apply_frame", apply_then_assign)
    split = (await _binding(app_session, w)).binding_split
    async with _http() as c:
        await _loosen_default(c, w.tenant, {"a": _RW, "b": _RW})
        seen = await _get(c, owner)
        assert seen["skills"] == []
        r = await _confirm(c, owner, seen["token"])
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {"error": "confirmation_changed"}

    dept = await _dept(app_session, w)
    assert dept.pending_frame_tools is not None
    assert _same(dept.frame["tools"]["b"], _R)
    assert (await _binding(app_session, w)).binding_split == split
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        assert await load_assigned_skills(db, agent=agent, tenant_id=w.tenant) == []
    assert await _events(app_session, w.tenant) == []


async def test_held_tools_that_no_longer_validate_are_refused(
    app_session: AppSessionFactory,
) -> None:
    """The held tools are validated again before they apply: one that no
    longer passes answers 422 and nothing applies."""
    w = await owned_world(app_session, tools={"a": _RW, "b": _R}, tool_key="a")
    async with app_session(w.tenant) as db:
        dept = await db.get(m.Department, w.department_id)
        assert dept is not None
        # An approval threshold on a connection with no value_spec.
        dept.pending_frame_tools = {"a": _RW, "b": {**_RW, "approval_eur": 50}}
    owner = await _owner(app_session, w)
    async with _http() as c:
        seen = await _get(c, owner)
        assert [t["key"] for t in seen["tools"]] == ["b"]
        r = await _confirm(c, owner, seen["token"])
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "value_spec_not_supported"

    dept = await _dept(app_session, w)
    assert dept.pending_frame_tools is not None
    assert _same(dept.frame["tools"]["b"], _R)
    assert await _events(app_session, w.tenant) == []


async def test_a_removed_key_is_never_listed_as_loosened() -> None:
    from oc8.departments.owner_confirmation import _loosened_tools

    assert _loosened_tools({"a": _R, "b": _R}, {"a": _R}) == []
    assert _loosened_tools({"a": _R}, {"a": _RW}) == ["a"]


# --- Fix round 2 (Ruling T9-I3): what changed at once is shown with content --


async def test_a_default_connection_change_is_shown(app_session: AppSessionFactory) -> None:
    """A field the policy does not read applies at once and breaks the
    confirmation: the agent's tools are shown with it, as the grant has them."""
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    w = await owned_world(
        app_session, tools={"a": {**_RW, "default_connection_id": first}}, tool_key="a"
    )
    async with _http() as c:
        r = await c.put(
            f"/api/v1/departments/{w.department_id}/tools",
            json={"tools": {"a": {**_RW, "defaultConnectionId": second}}},
            headers=_admin(w.tenant),
        )
        assert r.status_code == 200, r.text
        body = await _get(c, await _owner(app_session, w))
    (agent,) = body["agents"]
    assert "tools" in agent["changes"]
    assert agent["tools"]["a"]["frame_extra"] == {"default_connection_id": second}
    assert agent["tools"]["a"]["modify"] is True


async def test_a_held_tool_keeps_its_unread_fields(app_session: AppSessionFactory) -> None:
    login = str(uuid.uuid4())
    w = await owned_world(
        app_session, tools={"a": _RW, "b": {**_R, "default_connection_id": login}}, tool_key="a"
    )
    async with _http() as c:
        await _loosen_default(c, w.tenant, {"a": _RW, "b": {**_RW, "default_connection_id": login}})
        body = await _get(c, await _owner(app_session, w))
    (tool,) = body["tools"]
    assert tool["applied"]["default_connection_id"] == login
    assert tool["pending"]["default_connection_id"] == login


async def test_a_setup_change_is_shown_without_its_secrets(
    app_session: AppSessionFactory,
) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    async with app_session(w.tenant) as db:
        conn = m.McpConnection(
            tenant_id=w.tenant,
            department_id=w.department_id,
            name="a",
            server_url="https://one.example.test/mcp",
            transport="streamable_http",
            scopes=["read"],
            credential_id=w.credential_id,
            config={"api_key": "sk-live-SECRET-1234", "region": "eu"},
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
    await _restamp(app_session, w)
    async with app_session(w.tenant) as db:
        row = await db.get(m.McpConnection, conn_id)
        assert row is not None
        row.server_url = "https://two.example.test/mcp"
    async with _http() as c:
        r = await c.get(
            "/api/v1/personal-department/confirmation", headers=await _owner(app_session, w)
        )
    assert r.status_code == 200, r.text
    assert "sk-live-SECRET-1234" not in r.text
    (agent,) = r.json()["agents"]
    assert agent["changes"] == ["core"]
    (setup,) = [s for s in agent["setup"] if s["tool"] == "a"]
    assert setup["serverUrl"] == "https://two.example.test/mcp"
    assert setup["transport"] == "streamable_http"
    assert setup["scopes"] == ["read"]
    assert setup["configKeys"] == ["api_key", "region"]
    (login,) = setup["logins"]
    assert login["ownedByOwner"] is True and login["name"]


async def test_a_threshold_raised_during_the_confirmation_rolls_it_back(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's threshold is not under the department lock: one committed
    after the check is read by the stamps (the agents are read again) and
    the confirmation answers 409."""
    w = await owned_world(app_session, tools={"a": _RW, "b": _R}, tool_key="a")
    await _set_threshold(app_session, w, 100)
    await _restamp(app_session, w)
    owner = await _owner(app_session, w)
    from oc8.departments import owner_confirmation
    from oc8.departments.personal import _apply_frame as original

    async def raise_then_apply(*args: Any, **kwargs: Any) -> None:
        await _set_threshold(app_session, w, 5000)  # another transaction, committed
        await original(*args, **kwargs)

    monkeypatch.setattr(owner_confirmation, "_apply_frame", raise_then_apply)
    split = (await _binding(app_session, w)).binding_split
    async with _http() as c:
        await _loosen_default(c, w.tenant, {"a": _RW, "b": _RW})
        seen = await _get(c, owner)
        assert seen["agents"] == []
        r = await _confirm(c, owner, seen["token"])
        assert r.status_code == 409, r.text

    assert (await _dept(app_session, w)).pending_frame_tools is not None
    assert (await _binding(app_session, w)).binding_split == split
    assert await _events(app_session, w.tenant) == []
