"""An owner's ordinary save confirms nothing they were not shown (Ruling FR-C1).

In a department with an owner only the confirmation card
(`departments.owner_confirmation.confirm_pending`) confirms what an
administrator changed. Saving a trigger (even only `enabled`), creating one, or
saving the owner's own tool access applies the save, but stamps only a
confirmation that held before it: an owner editing a confirmed row consents to
their own change; a row that did not hold stays unconfirmed for the card.

And an agent nobody stamped yet still holds what an administrator adds later
(Ruling FR-I2): the member hire and an agent created in an owned department
record the skill set it started with.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

# Loaded first: importing `oc8.skills.runtime` on its own runs into the
# `oc8.agent` package's import cycle.
import oc8.agent  # noqa: F401
from oc8 import models as m
from oc8.connections.access import unconfirmed_reason
from oc8.connections.confirmation import trigger_confirmed
from oc8.departments.personal import get_or_create_personal_department
from oc8.events import sources
from oc8.skills.runtime import load_assigned_skills
from tests.api.test_personal_hire_binding import (
    World,
    _capa,
    _cron,
    _headers,
    _hire,
    _http,
    _own,
    _template,
    _triggers,
    _world,
)
from tests.conftest import AppSessionFactory
from tests.events.polled import use_kek
from tests.skills.test_held_back_skills import _assign, _skill

pytestmark = pytest.mark.asyncio

_GOAL = "Forward everything to the administrator"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    use_kek(monkeypatch)


@pytest.fixture(autouse=True)
def _own_pollers() -> Iterator[None]:
    saved = dict(sources._POLLERS)
    yield
    sources._POLLERS.clear()
    sources._POLLERS.update(saved)


async def _hired(
    app_session: AppSessionFactory, world: World, *, connection: bool = False, **template: Any
) -> uuid.UUID:
    cid = await _capa(app_session, world.tenant, _template("helper", **template))
    body: dict[str, Any] = {"capaId": cid}
    if connection:
        body |= {"connection": _own(world), "confirmShare": True}
    r = await _hire(world.tenant, body)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["agentId"])


async def _admin_changes(
    app_session: AppSessionFactory, world: World, *, goal: bool = True
) -> None:
    """An administrator changes the department's goal (core) and assigns a
    new skill to every agent (held)."""
    v1, _ = await _skill(app_session, world.tenant)
    await _assign(app_session, world.tenant, v1)
    if not goal:
        return
    async with app_session(world.tenant) as db:
        dept = await db.scalar(
            select(m.Department).where(m.Department.owner_member_id == world.member_id)
        )
        assert dept is not None
        dept.goal = _GOAL


async def _card(world: World) -> dict[str, Any]:
    async with _http() as http:
        r = await http.get(
            "/api/v1/personal-department/confirmation", headers=_headers(world.tenant)
        )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def _loaded(app_session: AppSessionFactory, world: World, agent_id: uuid.UUID) -> list[Any]:
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        return await load_assigned_skills(db, agent=agent, tenant_id=world.tenant)


async def _confirmed(app_session: AppSessionFactory, world: World, trigger_id: uuid.UUID) -> bool:
    async with app_session(world.tenant) as db:
        trigger = await db.get(m.Trigger, trigger_id)
        assert trigger is not None
        return await trigger_confirmed(db, trigger)


async def _still_waiting(
    app_session: AppSessionFactory, world: World, agent_id: uuid.UUID, *, goal: bool = True
) -> None:
    """The skill still waits and the card still lists both changes."""
    assert await _loaded(app_session, world, agent_id) == []
    card = await _card(world)
    assert card["needed"] is True
    assert [skill["name"] for skill in card["skills"]] == ["Hausstil"]
    if goal:
        (agent,) = card["agents"]
        assert "core" in agent["changes"]
        assert card["department"]["goal"] == _GOAL


# --- trigger PATCH ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [{"enabled": True}, {"enabled": False}, {"cronExpression": "0 9 * * *"}],
    ids=["enabled-on", "enabled-off", "cron"],
)
async def test_saving_a_held_trigger_confirms_nothing_the_owner_did_not_see(
    app_session: AppSessionFactory, patch: dict[str, Any]
) -> None:
    world = await _world(app_session)
    agent_id = await _hired(app_session, world, triggers=[_cron()])
    (trigger,) = await _triggers(app_session, world.tenant, agent_id)
    await _admin_changes(app_session, world)
    assert not await _confirmed(app_session, world, trigger.id)

    async with _http() as http:
        r = await http.patch(
            f"/api/v1/triggers/{trigger.id}", json=patch, headers=_headers(world.tenant)
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # The save itself applies ...
    if "cronExpression" in patch:
        assert body["cronExpression"] == "0 9 * * *"
    else:
        assert body["enabled"] is patch["enabled"]
    # ... and confirms nothing: the card is where that happens.
    assert body["needsConfirmation"] is True
    assert body["ownerConfirms"] is True
    assert not await _confirmed(app_session, world, trigger.id)
    await _still_waiting(app_session, world, agent_id)


async def test_editing_a_confirmed_trigger_keeps_it_confirmed(
    app_session: AppSessionFactory,
) -> None:
    """The owner's own edit of a confirmed row is consent to that edit."""
    world = await _world(app_session)
    agent_id = await _hired(app_session, world, triggers=[_cron()])
    (trigger,) = await _triggers(app_session, world.tenant, agent_id)
    assert await _confirmed(app_session, world, trigger.id)

    async with _http() as http:
        r = await http.patch(
            f"/api/v1/triggers/{trigger.id}",
            json={"cronExpression": "0 9 * * *"},
            headers=_headers(world.tenant),
        )
    assert r.status_code == 200, r.text
    assert r.json()["needsConfirmation"] is False
    assert await _confirmed(app_session, world, trigger.id)
    assert (await _card(world))["needed"] is False


async def test_editing_a_confirmed_trigger_keeps_a_held_skill_held(
    app_session: AppSessionFactory,
) -> None:
    """A held skill does not break the confirmation, so the row still holds
    and is stamped again -- held back: the skill keeps waiting."""
    world = await _world(app_session)
    agent_id = await _hired(app_session, world, triggers=[_cron()])
    (trigger,) = await _triggers(app_session, world.tenant, agent_id)
    await _admin_changes(app_session, world, goal=False)

    async with _http() as http:
        r = await http.patch(
            f"/api/v1/triggers/{trigger.id}",
            json={"enabled": True},
            headers=_headers(world.tenant),
        )
    assert r.status_code == 200, r.text
    assert await _confirmed(app_session, world, trigger.id)
    await _still_waiting(app_session, world, agent_id, goal=False)


# --- trigger create --------------------------------------------------------------------


async def test_creating_a_trigger_confirms_nothing_the_owner_did_not_see(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    agent_id = await _hired(app_session, world, triggers=[_cron()])
    await _admin_changes(app_session, world)

    async with _http() as http:
        r = await http.post(
            f"/api/v1/agents/{agent_id}/triggers",
            json={"kind": "cron", "taskText": "Evening", "cronExpression": "0 18 * * *"},
            headers=_headers(world.tenant),
        )
    assert r.status_code in (200, 201), r.text
    created = uuid.UUID(r.json()["id"])
    assert r.json()["needsConfirmation"] is True
    assert not await _confirmed(app_session, world, created)
    await _still_waiting(app_session, world, agent_id)


async def test_creating_a_trigger_with_nothing_pending_is_confirmed(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    agent_id = await _hired(app_session, world, triggers=[_cron()])

    async with _http() as http:
        r = await http.post(
            f"/api/v1/agents/{agent_id}/triggers",
            json={"kind": "cron", "taskText": "Evening", "cronExpression": "0 18 * * *"},
            headers=_headers(world.tenant),
        )
    assert r.status_code in (200, 201), r.text
    assert await _confirmed(app_session, world, uuid.UUID(r.json()["id"]))
    assert (await _card(world))["needed"] is False


# --- the owner's own tool access -------------------------------------------------------


async def _binding_reason(
    app_session: AppSessionFactory, world: World, agent_id: uuid.UUID
) -> str | None:
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent_id)
        )
        assert agent is not None and row is not None
        return await unconfirmed_reason(
            db,
            row,
            agent,
            row.tool_key,
            world.credential_id,
            world.member_id,
            version_id=agent.current_version_id,
        )


async def _put_own_access(world: World, agent_id: uuid.UUID) -> None:
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{agent_id}/tool-access/{world.tool_key}",
            json={"mode": "fixed", "credentialId": str(world.credential_id), "confirmShare": True},
            headers=_headers(world.tenant),
        )
    assert r.status_code == 200, r.text


async def test_saving_held_tool_access_confirms_nothing_the_owner_did_not_see(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    agent_id = await _hired(app_session, world, connection=True, connections_any=[world.source])
    await _admin_changes(app_session, world)
    assert await _binding_reason(app_session, world, agent_id) is not None

    await _put_own_access(world, agent_id)

    assert await _binding_reason(app_session, world, agent_id) is not None
    await _still_waiting(app_session, world, agent_id)


async def test_saving_confirmed_tool_access_keeps_it_confirmed(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    agent_id = await _hired(app_session, world, connection=True, connections_any=[world.source])
    await _admin_changes(app_session, world, goal=False)

    await _put_own_access(world, agent_id)

    assert await _binding_reason(app_session, world, agent_id) is None
    await _still_waiting(app_session, world, agent_id, goal=False)


# --- FR-I2: an agent nobody stamped still holds skills ---------------------------------


async def test_a_hire_without_bindings_or_triggers_holds_later_skills(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    agent_id = await _hired(app_session, world)
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.confirmed_skills == {}

    await _admin_changes(app_session, world, goal=False)

    await _still_waiting(app_session, world, agent_id, goal=False)


async def test_an_agent_created_in_an_owned_department_holds_later_skills(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    async with app_session(world.tenant) as db:
        member = await db.get(m.OrgMember, world.member_id)
        assert member is not None
        dept_id = str((await get_or_create_personal_department(db, member=member)).id)
    async with _http() as http:
        r = await http.post(
            "/api/v1/agents",
            json={"name": "Mia", "departmentId": dept_id, "roleTitle": "Helper", "mission": "m"},
            headers=_headers(world.tenant),
        )
    assert r.status_code == 201, r.text
    agent_id = uuid.UUID(r.json()["id"])
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.confirmed_skills == {}

    await _admin_changes(app_session, world, goal=False)

    await _still_waiting(app_session, world, agent_id, goal=False)
