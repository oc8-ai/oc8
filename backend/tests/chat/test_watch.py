"""Who may watch a run on a member-only socket -- and, the security half, who
may NOT once they lost the run's agent (binding ruling U2/4)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.audience import session_audience
from oc8.chat.reach import member_can_reach_agent
from oc8.chat.watch import may_watch_run
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import Seat, copilot_seat

pytestmark = pytest.mark.asyncio


async def _chat_run(db: AsyncSession, tenant: uuid.UUID, seat: Seat) -> m.AgentRun:
    session = await db.get(m.ChatSession, seat.session_id)
    assert session is not None
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=session.agent_id,
        source="chat",
        context={"chat_session_id": str(seat.session_id), "originating_operator": seat.subject},
    )
    db.add(run)
    await db.flush()
    return run


async def _member(db: AsyncSession, member_id: uuid.UUID) -> m.OrgMember:
    row = await db.get(m.OrgMember, member_id)
    assert row is not None
    return row


async def test_owner_may_watch_their_chat_run_and_others_may_not(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        bob = await copilot_seat(db, tenant, "bob")
        run = await _chat_run(db, tenant, alice)
        alice_row = await _member(db, alice.member_id)
        bob_row = await _member(db, bob.member_id)
        assert await may_watch_run(db, member=alice_row, run_id=run.id)
        assert not await may_watch_run(db, member=bob_row, run_id=run.id)
        assert not await may_watch_run(db, member=bob_row, run_id=uuid.uuid4())


async def test_operator_who_lost_agent_access_gets_no_events(
    app_session: AppSessionFactory,
) -> None:
    """The originating-operator branch alone is not enough: a run against a
    departmental agent the member cannot (any longer) see stays unwatchable,
    even though they started it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        dept = m.Department(tenant_id=tenant, name="Finanzen")
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Fin", status="running")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="chat",
            context={"originating_operator": "alice"},
        )
        db.add(run)
        await db.flush()
        alice_row = await _member(db, alice.member_id)
        assert not await member_can_reach_agent(db, member=alice_row, agent_id=agent.id)
        assert not await may_watch_run(db, member=alice_row, run_id=run.id)
        # With a seat in the department she may.
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=alice_row.id,
                department_id=dept.id,
                seat_role="dept_viewer",
            )
        )
        await db.flush()
        assert await may_watch_run(db, member=alice_row, run_id=run.id)


async def test_session_owner_who_lost_agent_access_may_not_watch_or_be_told(
    app_session: AppSessionFactory,
) -> None:
    """Both branches -- operator and session owner -- re-check the agent."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        run = await _chat_run(db, tenant, alice)
        session = await db.get(m.ChatSession, alice.session_id)
        assert session is not None
        alice_row = await _member(db, alice.member_id)
        assert await session_audience(db, session=session) == [alice.member_id]
        # Her role (the copilot:use grant) is taken away.
        alice_row.role_id = None
        await db.flush()
        assert not await may_watch_run(db, member=alice_row, run_id=run.id)
        assert await session_audience(db, session=session) == []


async def test_an_offboarded_member_reaches_nothing(app_session: AppSessionFactory) -> None:
    import datetime as dt

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        run = await _chat_run(db, tenant, alice)
        alice_row = await _member(db, alice.member_id)
        alice_row.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        assert not await may_watch_run(db, member=alice_row, run_id=run.id)


async def test_the_socket_recheck_reads_access_afresh(app_session: AppSessionFactory) -> None:
    """`ws.py`'s re-check is the same predicate, re-read from the database:
    allowed, then (role taken away) lost access, then (offboarded) gone."""
    import datetime as dt

    from oc8.realtime.ws import _watch_recheck

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        run = await _chat_run(db, tenant, alice)
        run_id = str(run.id)
    recheck = _watch_recheck(tenant, alice.member_id, None)
    assert await recheck(run_id) == "ok"
    async with app_session(tenant) as db:
        row = await _member(db, alice.member_id)
        row.role_id = None
    assert await recheck(run_id) == "access"
    async with app_session(tenant) as db:
        row = await _member(db, alice.member_id)
        row.deleted_at = dt.datetime.now(tz=dt.UTC)
    assert await recheck(run_id) == "gone"


async def _dept_agent(db: AsyncSession, tenant: uuid.UUID) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
    db.add(agent)
    await db.flush()
    return agent


async def _seated(
    db: AsyncSession, tenant: uuid.UUID, subject: str, agent: m.Agent, *, manage: bool | None
) -> m.OrgMember:
    """`manage=None`: no seat at all."""
    member = m.OrgMember(tenant_id=tenant, subject=subject, subject_uuid=uuid.uuid4())
    db.add(member)
    await db.flush()
    if manage is not None:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=member.id,
                department_id=agent.department_id,
                seat_role="dept_viewer",
                agent_manage=manage,
            )
        )
        await db.flush()
    return member


async def test_a_member_who_reaches_the_agents_runs_may_watch_its_scheduled_runs(
    app_session: AppSessionFactory,
) -> None:
    """The Live timeline shows autonomous runs to whoever reaches the agent
    AND its runs (`run:view`, run reach), so their live events follow. A seat
    alone reaches the agent, not its runs."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _dept_agent(db, tenant)
        stranger = await _seated(db, tenant, "s", agent, manage=None)
        viewer = await _seated(db, tenant, "v", agent, manage=False)
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="cron", context={})
        db.add(run)
        await db.flush()
        # No role, no seat: sees no department, so no scheduled run either.
        assert not await may_watch_run(db, member=stranger, run_id=run.id)
        # A seat reaches the agent, but a plain member holds no `run:view`.
        assert not await may_watch_run(db, member=viewer, run_id=run.id, token_role="member")
        # An operator reads every agent's runs.
        assert await may_watch_run(db, member=viewer, run_id=run.id, token_role="operator")


async def test_a_colleagues_run_is_never_watched_live_not_even_by_a_manager(
    app_session: AppSessionFactory,
) -> None:
    """The live stream (token deltas, tool arguments, reasoning) carries the
    colleague's words: a manager sees such a run only afterwards, redacted,
    through "All runs" -- never live. That holds for a chat run, a sub-run
    delegated from it, and a run the colleague started by hand."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _dept_agent(db, tenant)
        owner = await _seated(db, tenant, "owner", agent, manage=False)
        viewer = await _seated(db, tenant, "v", agent, manage=False)
        manager = await _seated(db, tenant, "boss", agent, manage=True)
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=owner.id)
        db.add(session)
        await db.flush()
        ctx = {"chat_session_id": str(session.id), "originating_operator": "owner"}
        chat_run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="chat", context=ctx)
        sub_run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="delegation", context=ctx)
        by_hand = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="manual",
            context={"originating_operator": "owner"},
        )
        db.add_all([chat_run, sub_run, by_hand])
        await db.flush()
        for run in (chat_run, sub_run, by_hand):
            assert await may_watch_run(db, member=owner, run_id=run.id)
            assert not await may_watch_run(db, member=viewer, run_id=run.id)
            assert not await may_watch_run(db, member=manager, run_id=run.id)
