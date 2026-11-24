"""A login that is gone only takes away (ruling T12-N2): when the
owner deletes the login their agent is fixed to, the binding and their
triggers stay confirmed -- the run refuses the tool, it never re-chooses --
and nothing waits for their confirmation. A new or different login in its
place never holds without their consent."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.connections.access import ConnectionRequired, resolve_tool_access
from oc8.connections.access_write import write_tool_access
from oc8.connections.confirmation import (
    confirmation_holds,
    owner_confirmations,
    trigger_confirmed,
)
from oc8.credentials.service import delete_credential
from oc8.departments import owner_confirmation
from oc8.departments.owner_confirmation import (
    ConfirmationChanged,
    confirm_pending,
    pending_confirmation,
)
from oc8.triggers.service import create_trigger
from tests.conftest import AppSessionFactory
from tests.connections.helpers import credential_row, enable_plugin_row, member
from tests.departments.owned import OwnedWorld, owned_world

pytestmark = pytest.mark.asyncio

ODOO_CFG: dict[str, Any] = {
    "_plugin_name": "odoo_mcp",
    "_connection_key": "primary",
    "command": "uv",
    "args": ["tool", "run", "mcp-server-odoo"],
    "env": {},
    "secret_env": {},
}


async def _world(app_session: AppSessionFactory) -> tuple[OwnedWorld, uuid.UUID, uuid.UUID]:
    """A personal department's agent with the organisation's shared Odoo
    tool, fixed by its owner to the owner's own login, and a schedule the
    owner created: (world, the shared tool's row, the trigger)."""
    world = await owned_world(
        app_session, tools={"odoo": {"enabled": True, "read": True, "modify": True}}
    )
    assert world.agent_id is not None
    await enable_plugin_row(app_session, world.tenant, "odoo_mcp")
    async with app_session(world.tenant) as db:
        conn = m.McpConnection(
            tenant_id=world.tenant,
            name="odoo",
            server_url="",
            transport="stdio",
            config=dict(ODOO_CFG),
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
    credential_id = await credential_row(app_session, world.tenant, owner=world.member_id)
    await _bind(app_session, world, credential_id)
    async with app_session(world.tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=world.tenant,
            agent_id=world.agent_id,
            kind="cron",
            task_text="Tidy up",
            cron_expression="0 7 * * *",
            created_by_member_id=world.member_id,
            acts_for_creator=True,
        )
        trigger_id = trigger.id
    world = OwnedWorld(
        world.tenant, world.member_id, world.department_id, world.agent_id, credential_id
    )
    return world, conn_id, trigger_id


async def _bind(
    app_session: AppSessionFactory, world: OwnedWorld, credential_id: uuid.UUID
) -> None:
    """The owner fixes the tool to their login: their confirmation."""
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, world.agent_id)
        assert agent is not None
        await write_tool_access(
            db,
            agent=agent,
            tool_key="odoo",
            mode="fixed",
            oauth_connection_id=None,
            credential_id=credential_id,
            set_by_member_id=world.member_id,
            set_with_manage=False,
            actor_type="operator",
            actor_id=world.member_id,
        )


async def _delete_login(app_session: AppSessionFactory, world: OwnedWorld) -> None:
    assert world.credential_id is not None
    async with app_session(world.tenant) as db:
        await delete_credential(
            db, tenant_id=world.tenant, credential_id=world.credential_id, force=True
        )


async def _access_row(db: AsyncSession, world: OwnedWorld) -> m.AgentToolAccess:
    row = await db.scalar(
        select(m.AgentToolAccess).where(
            m.AgentToolAccess.agent_id == world.agent_id, m.AgentToolAccess.tool_key == "odoo"
        )
    )
    assert row is not None
    return row


async def _binding_holds(app_session: AppSessionFactory, world: OwnedWorld) -> bool:
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, world.agent_id)
        assert agent is not None
        stored = await _access_row(db, world)
        return await confirmation_holds(
            db,
            agent,
            "odoo",
            version_id=agent.current_version_id,
            digest=stored.binding_fingerprint,
            split=stored.binding_split,
        )


async def _trigger_holds(app_session: AppSessionFactory, world: OwnedWorld, tid: uuid.UUID) -> bool:
    async with app_session(world.tenant) as db:
        trigger = await db.get(m.Trigger, tid)
        assert trigger is not None
        return await trigger_confirmed(db, trigger)


async def _resolve(app_session: AppSessionFactory, world: OwnedWorld, conn_id: uuid.UUID) -> Any:
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, world.agent_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert agent is not None and conn is not None
        run = m.AgentRun(
            tenant_id=world.tenant,
            agent_id=agent.id,
            source="manual",
            context={"originating_operator": "someone"},
        )
        db.add(run)
        await db.flush()
        return await resolve_tool_access(db, run=run, agent=agent, conn=conn)


async def _set_target(
    app_session: AppSessionFactory, world: OwnedWorld, credential_id: uuid.UUID
) -> None:
    """The row now points at `credential_id`, with nobody's new consent."""
    async with app_session(world.tenant) as db:
        (await _access_row(db, world)).credential_id = credential_id


async def test_deleting_the_login_keeps_the_confirmations_and_refuses_the_tool(
    app_session: AppSessionFactory,
) -> None:
    world, conn_id, tid = await _world(app_session)
    assert await _binding_holds(app_session, world)
    assert await _trigger_holds(app_session, world, tid)

    await _delete_login(app_session, world)

    assert await _binding_holds(app_session, world)
    assert await _trigger_holds(app_session, world, tid)
    async with app_session(world.tenant) as db:
        dept = await db.get(m.Department, world.department_id)
        assert dept is not None
        assert (await pending_confirmation(db, dept)).needed is False
    # The run refuses the tool: a gone login is never re-chosen.
    with pytest.raises(ConnectionRequired) as refused:
        await _resolve(app_session, world, conn_id)
    assert refused.value.reason == "not_connected"


async def test_a_new_login_in_its_place_needs_consent(app_session: AppSessionFactory) -> None:
    world, conn_id, tid = await _world(app_session)
    await _delete_login(app_session, world)
    fresh = await credential_row(app_session, world.tenant, owner=world.member_id, name="New")

    await _set_target(app_session, world, fresh)

    assert not await _binding_holds(app_session, world)
    assert not await _trigger_holds(app_session, world, tid)
    async with app_session(world.tenant) as db:
        dept = await db.get(m.Department, world.department_id)
        assert dept is not None
        assert (await pending_confirmation(db, dept)).needed is True
    with pytest.raises(ConnectionRequired) as held:
        await _resolve(app_session, world, conn_id)
    assert held.value.reason == "binding_needs_confirmation"


async def test_a_different_login_in_its_place_needs_consent(
    app_session: AppSessionFactory,
) -> None:
    world, conn_id, tid = await _world(app_session)
    other = await credential_row(app_session, world.tenant, owner=world.member_id, name="Other")

    await _set_target(app_session, world, other)

    assert not await _binding_holds(app_session, world)
    await _assert_held(app_session, world, conn_id, tid)


async def test_the_owner_binding_the_new_login_confirms_it(
    app_session: AppSessionFactory,
) -> None:
    world, _conn_id, tid = await _world(app_session)
    await _delete_login(app_session, world)
    fresh = await credential_row(app_session, world.tenant, owner=world.member_id, name="New")

    await _bind(app_session, world, fresh)

    assert await _binding_holds(app_session, world)
    # The trigger was confirmed with the old login: the new one waits for
    # the owner there too.
    assert not await _trigger_holds(app_session, world, tid)


# --- a login swapped in by someone else is held (Ruling T12-I1) -------------


async def _needed(app_session: AppSessionFactory, world: OwnedWorld) -> Any:
    async with app_session(world.tenant) as db:
        dept = await db.get(m.Department, world.department_id)
        assert dept is not None
        return await pending_confirmation(db, dept)


async def _assert_held(
    app_session: AppSessionFactory, world: OwnedWorld, conn_id: uuid.UUID, tid: uuid.UUID
) -> None:
    """The trigger and the binding wait for the owner, and the run is held."""
    assert not await _trigger_holds(app_session, world, tid)
    assert (await _needed(app_session, world)).needed is True
    with pytest.raises(ConnectionRequired) as held:
        await _resolve(app_session, world, conn_id)
    assert held.value.reason == "binding_needs_confirmation"


async def test_an_organisation_login_swapped_in_is_held(app_session: AppSessionFactory) -> None:
    world, conn_id, tid = await _world(app_session)
    org = await credential_row(app_session, world.tenant, owner=None, name="Shared")

    await _set_target(app_session, world, org)

    assert not await _binding_holds(app_session, world)
    await _assert_held(app_session, world, conn_id, tid)


async def test_an_admin_binding_a_new_login_is_held(app_session: AppSessionFactory) -> None:
    world, conn_id, tid = await _world(app_session)
    admin = await member(app_session, world.tenant, f"admin-{uuid.uuid4().hex[:6]}@acme.test")
    fresh = await credential_row(app_session, world.tenant, owner=world.member_id, name="New")

    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, world.agent_id)
        assert agent is not None
        await write_tool_access(
            db,
            agent=agent,
            tool_key="odoo",
            mode="fixed",
            oauth_connection_id=None,
            credential_id=fresh,
            set_by_member_id=admin,
            set_with_manage=False,
            actor_type="operator",
            actor_id=admin,
        )

    # Stamped anew by the admin's write, but not chosen by the owner.
    await _assert_held(app_session, world, conn_id, tid)


async def test_a_frame_and_login_change_shows_the_new_login(
    app_session: AppSessionFactory,
) -> None:
    """A login change sits outside `core`, yet the owner is shown it as a
    setup change (`login:` -> core), next to the frame's."""
    world, _conn_id, _tid = await _world(app_session)
    org = await credential_row(app_session, world.tenant, owner=None, name="Shared mailbox")
    async with app_session(world.tenant) as db:
        dept = await db.get(m.Department, world.department_id)
        assert dept is not None
        dept.frame = {**(dept.frame or {}), "kbs": ["kb-new"]}
    await _set_target(app_session, world, org)

    summary = await _needed(app_session, world)

    (agent,) = summary.agents
    assert {"core", "frame"} <= set(agent.changes)
    (setup,) = [view for view in agent.setup or [] if view.tool == "odoo"]
    assert [login.name for login in setup.logins] == ["Shared mailbox"]


async def test_a_login_swapped_during_the_confirmation_rolls_it_back(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admin replaces the owner's binding with one on another login after
    the token check: the trigger's stamp would record a login the owner
    never saw, so the confirmation is refused and nothing is written."""
    world, _conn_id, tid = await _world(app_session)
    admin = await member(app_session, world.tenant, f"admin-{uuid.uuid4().hex[:6]}@acme.test")
    org = await credential_row(app_session, world.tenant, owner=None, name="Shared")
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, world.agent_id)
        assert agent is not None
        agent.mission = "a new mission"
        await db.flush()
        await publish_version(db, agent)
    seen = await _needed(app_session, world)
    assert seen.needed
    async with app_session(world.tenant) as db:
        before = await db.get(m.Trigger, tid)
        assert before is not None
        split = before.access_split

    original = owner_confirmations
    calls = 0

    async def swap_then_list(db: Any, agent: m.Agent, owner: uuid.UUID) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:  # after the survey, before the stamps
            async with app_session(world.tenant) as other:
                await other.delete(await _access_row(other, world))
                await other.flush()
                other.add(
                    m.AgentToolAccess(
                        tenant_id=world.tenant,
                        agent_id=world.agent_id,
                        tool_key="odoo",
                        mode="fixed",
                        credential_id=org,
                        set_by_member_id=admin,
                        set_with_manage=True,
                    )
                )
        return await original(db, agent, owner)

    monkeypatch.setattr(owner_confirmation, "owner_confirmations", swap_then_list)
    with pytest.raises(ConfirmationChanged):
        async with app_session(world.tenant) as db:
            dept = await db.get(m.Department, world.department_id)
            owner = await db.get(m.OrgMember, world.member_id)
            assert dept is not None and owner is not None
            await confirm_pending(db, dept, member=owner, token=seen.token)

    assert calls >= 2
    async with app_session(world.tenant) as db:
        after = await db.get(m.Trigger, tid)
        assert after is not None
        assert after.access_split == split
    assert not await _trigger_holds(app_session, world, tid)
