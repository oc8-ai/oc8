"""The view the PEP decides every Copilot call against, reloaded per call."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.harness.stages.b_direct_access import NO_DIRECT_ACCESS
from oc8.connections.direct_access import (
    active_grant,
    direct_access_run,
    direct_connections,
    load_direct_access,
    note_direct_action,
    revoke_grant,
)
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import READ_ONLY, chat_run, direct_office, grant

pytestmark = pytest.mark.asyncio


async def _copilot(db: AsyncSession, copilot_id: uuid.UUID) -> m.Agent:
    agent = await db.get(m.Agent, copilot_id)
    assert agent is not None
    return agent


async def test_a_present_member_with_a_grant_sees_the_connection(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        run = await chat_run(db, office)
        agent = await _copilot(db, office.copilot_id)
        view = await load_direct_access(db, run=run, agent=agent)
        assert view.member_id == office.member_id
        assert [d for d, _p in view.granted["things"]] == [office.department_id]
        conns = await direct_connections(db, run=run, agent=agent)
        assert [c.id for c in conns] == [office.connection_id]


async def test_a_grant_in_another_conversation_does_not_count(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        other = m.ChatSession(
            tenant_id=tenant, agent_id=office.copilot_id, member_id=office.member_id
        )
        db.add(other)
        await db.flush()
        await grant(db, office, session_id=other.id)
        run = await chat_run(db, office)
        view = await load_direct_access(db, run=run, agent=await _copilot(db, office.copilot_id))
        assert view.granted == {}


async def test_a_revoked_grant_or_lost_seat_leaves_nothing_granted(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        row = await grant(db, office)
        run = await chat_run(db, office)
        agent = await _copilot(db, office.copilot_id)
        # Same run, same objects: each load reads the database afresh.
        assert "things" in (await load_direct_access(db, run=run, agent=agent)).granted
        await revoke_grant(db, grant=row, member_id=office.member_id)
        assert (await load_direct_access(db, run=run, agent=agent)).granted == {}
        assert await active_grant(db, session_id=office.session_id, tool_key="things") is None

        await grant(db, office)
        assert "things" in (await load_direct_access(db, run=run, agent=agent)).granted
        seat = (
            await db.execute(
                select(m.OrgMemberDepartment).where(
                    m.OrgMemberDepartment.member_id == office.member_id
                )
            )
        ).scalar_one()
        seat.revoked_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert (await load_direct_access(db, run=run, agent=agent)).granted == {}


@pytest.mark.parametrize(
    ("door", "on_behalf", "source", "extra"),
    [
        ("followup", False, "chat", {"followup": {"trigger_id": "x"}}),
        ("telegram", False, "chat", {}),
        ("web", True, "delegation", {}),
        ("web", True, "chat", {"acting_person_withheld": "someone_elses_words"}),
        ("web", True, "chat", {"trigger_id": str(uuid.uuid4())}),
        ("web", False, "chat", {}),
    ],
)
async def test_only_a_live_web_turn_of_the_member_is_a_direct_access_run(
    app_session: AppSessionFactory,
    door: str,
    on_behalf: bool,
    source: str,
    extra: dict[str, Any],
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        run = await chat_run(db, office, door=door, on_behalf=on_behalf, source=source, extra=extra)
        agent = await _copilot(db, office.copilot_id)
        assert direct_access_run(run, agent) is None
        assert await load_direct_access(db, run=run, agent=agent) == NO_DIRECT_ACCESS
        assert await direct_connections(db, run=run, agent=agent) == []


async def test_an_ordinary_agent_is_never_a_direct_access_run(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run = await chat_run(db, office)
        agent = await _copilot(db, office.copilot_id)
        agent.is_tenant_assistant = False
        assert direct_access_run(run, agent) is None


async def test_the_second_successful_action_suggests_an_agent_once(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        row = await grant(db, office)
        run = await chat_run(db, office)
        tool = "search_records"
        assert (
            await note_direct_action(db, run=run, tool_key="things", tool=tool, succeeded=False)
            is False
        )
        assert (
            await note_direct_action(db, run=run, tool_key="things", tool=tool, succeeded=True)
            is False
        )
        assert (
            await note_direct_action(db, run=run, tool_key="things", tool=tool, succeeded=True)
            is True
        )
        assert (
            await note_direct_action(db, run=run, tool_key="things", tool=tool, succeeded=True)
            is False
        )
        await db.refresh(row)
        assert row.actions == 3 and row.agent_suggested_at is not None


async def test_the_view_follows_a_frame_changed_in_another_session(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as first:
        office = await direct_office(first, tenant)
        await grant(first, office)
        run = await chat_run(first, office)
        await first.commit()
        await first.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
        )
        # Held strongly, as a running turn holds them (the identity map is weak).
        held = [
            await first.get(m.Department, office.department_id),
            await first.get(m.OrgMember, office.member_id),
            await first.get(m.ChatSession, office.session_id),
        ]
        agent = await _copilot(first, office.copilot_id)
        view = await load_direct_access(first, run=run, agent=agent)
        assert view.granted["things"][0][1].modify is True
        async with app_session(tenant) as second:
            dept = await second.get(m.Department, office.department_id)
            assert dept is not None
            dept.frame = {"tools": {"things": dict(READ_ONLY)}}
        view = await load_direct_access(first, run=run, agent=agent)
        assert view.granted["things"][0][1].modify is False
        async with app_session(tenant) as second:
            dept = await second.get(m.Department, office.department_id)
            assert dept is not None
            dept.frame = {"tools": {"things": {"enabled": False}}}
        assert (await load_direct_access(first, run=run, agent=agent)).granted == {}
        assert all(row is not None for row in held)
