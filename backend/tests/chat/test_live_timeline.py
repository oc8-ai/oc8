"""The Live timeline (spec §3.1): the member's own live turns merged at read
time with the agent's autonomous runs -- and, the privacy half, never a run
started from another member's chat unless a manager asks for "All runs", and
even then never that member's text."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.live import (
    LiveItem,
    get_or_create_live_session,
    is_autonomous_run,
    live_timeline,
    trigger_of,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _world(db: AsyncSession, tenant: uuid.UUID) -> tuple[m.Agent, m.OrgMember, m.OrgMember]:
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
    alice = m.OrgMember(
        tenant_id=tenant, subject="alice", subject_uuid=uuid.uuid4(), display_name="Alice"
    )
    bob = m.OrgMember(
        tenant_id=tenant, subject="bob", subject_uuid=uuid.uuid4(), display_name="Bob"
    )
    db.add_all([agent, alice, bob])
    await db.flush()
    return agent, alice, bob


def _run(
    tenant: uuid.UUID, agent: m.Agent, source: str, context: dict[str, Any], **kw: Any
) -> m.AgentRun:
    return m.AgentRun(
        tenant_id=tenant, agent_id=agent.id, source=source, state="done", context=context, **kw
    )


async def _bob_chat(
    db: AsyncSession, tenant: uuid.UUID, agent: m.Agent, bob: m.OrgMember
) -> tuple[m.ChatSession, m.AgentRun]:
    session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=bob.id)
    db.add(session)
    await db.flush()
    db.add(
        m.ChatMessage(tenant_id=tenant, session_id=session.id, role="user", content="BOB SECRET")
    )
    run = _run(
        tenant,
        agent,
        "chat",
        {
            "chat_session_id": str(session.id),
            "originating_operator": "bob",
            "task": "User: BOB SECRET",
            "output": "answered bob",
        },
    )
    db.add(run)
    await db.flush()
    return session, run


def _run_ids(items: list[LiveItem]) -> set[uuid.UUID]:
    return {i.run.id for i in items if i.run is not None}


async def test_one_live_session_per_member_and_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, bob = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        again = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        bobs = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=bob.id, agent_id=agent.id
        )
    assert again.id == live.id
    assert live.kind == "live"
    assert bobs.id != live.id


async def test_timeline_merges_turns_and_autonomous_runs_never_others_chat_text(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, bob = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        db.add(m.ChatMessage(tenant_id=tenant, session_id=live.id, role="user", content="status?"))
        cron = _run(tenant, agent, "cron", {"task": "check inbox", "output": "2 new"})
        db.add(cron)
        await db.flush()
        _, bob_run = await _bob_chat(db, tenant, agent, bob)

        plain = await live_timeline(
            db, member=alice, agent=agent, session=live, before=None, limit=50, all_runs=False
        )
        everything = await live_timeline(
            db, member=alice, agent=agent, session=live, before=None, limit=50, all_runs=True
        )

    assert [i.kind for i in plain] == ["message", "run"]
    assert _run_ids(plain) == {cron.id}
    assert _run_ids(everything) == {cron.id, bob_run.id}
    for item in everything:
        if item.message is not None:
            assert "BOB SECRET" not in item.message.content


async def test_a_sub_run_delegated_from_another_members_chat_counts_as_that_members(
    app_session: AppSessionFactory,
) -> None:
    """Delegation from Bob's chat carries his chat_session_id and operator:
    it is Bob's chat, not an autonomous run -- absent unless "All runs"."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, bob = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        bob_session, _ = await _bob_chat(db, tenant, agent, bob)
        from_bobs_chat = _run(
            tenant,
            agent,
            "delegation",
            {
                "chat_session_id": str(bob_session.id),
                "originating_operator": "bob",
                "task": "Bob asked: BOB SECRET",
            },
        )
        autonomous = _run(tenant, agent, "delegation", {"task": "nightly sub-task"})
        db.add_all([from_bobs_chat, autonomous])
        await db.flush()

        plain = await live_timeline(
            db, member=alice, agent=agent, session=live, before=None, limit=50, all_runs=False
        )
        everything = await live_timeline(
            db, member=alice, agent=agent, session=live, before=None, limit=50, all_runs=True
        )

    assert _run_ids(plain) == {autonomous.id}
    assert from_bobs_chat.id in _run_ids(everything)


async def test_the_members_own_chat_runs_are_never_run_items(
    app_session: AppSessionFactory,
) -> None:
    """Her live-session runs ARE the assistant messages; her other chats are
    not this timeline's business -- even with "All runs"."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, _ = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        other = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=alice.id)
        db.add(other)
        await db.flush()
        own_live = _run(
            tenant,
            agent,
            "chat",
            {"chat_session_id": str(live.id), "originating_operator": "alice"},
        )
        own_other = _run(
            tenant,
            agent,
            "chat",
            {"chat_session_id": str(other.id), "originating_operator": "alice"},
        )
        db.add_all([own_live, own_other])
        await db.flush()
        for all_runs in (False, True):
            items = await live_timeline(
                db,
                member=alice,
                agent=agent,
                session=live,
                before=None,
                limit=50,
                all_runs=all_runs,
            )
            assert _run_ids(items) == set()


async def test_runs_of_other_agents_are_not_on_this_timeline(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, _ = await _world(db, tenant)
        other = m.Agent(tenant_id=tenant, department_id=agent.department_id, name="B")
        db.add(other)
        await db.flush()
        db.add(_run(tenant, other, "cron", {}))
        await db.flush()
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        items = await live_timeline(
            db, member=alice, agent=agent, session=live, before=None, limit=50, all_runs=True
        )
    assert items == []


async def test_before_and_limit_page_backwards(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, _ = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        base = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
        for i in range(5):
            db.add(
                m.ChatMessage(
                    tenant_id=tenant,
                    session_id=live.id,
                    role="user",
                    content=f"m{i}",
                    created_at=base + dt.timedelta(minutes=i),
                )
            )
        await db.flush()
        page = await live_timeline(
            db,
            member=alice,
            agent=agent,
            session=live,
            before=base + dt.timedelta(minutes=4),
            limit=2,
            all_runs=False,
        )
    assert [i.message.content for i in page if i.message] == ["m2", "m3"]


async def test_paging_is_stable_when_items_share_a_timestamp(
    app_session: AppSessionFactory,
) -> None:
    """Ordered by (created_at, id) across both kinds: walking back page by
    page meets every item exactly once, ties included."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, _ = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        same = dt.datetime(2026, 11, 2, tzinfo=dt.UTC)
        for i in range(4):
            db.add(
                m.ChatMessage(
                    tenant_id=tenant,
                    session_id=live.id,
                    role="user",
                    content=f"m{i}",
                    created_at=same,
                )
            )
            db.add(_run(tenant, agent, "cron", {}, created_at=same))
        await db.flush()
        seen: list[tuple[str, uuid.UUID]] = []
        before: dt.datetime | None = None
        before_id: uuid.UUID | None = None
        for _ in range(10):
            page = await live_timeline(
                db,
                member=alice,
                agent=agent,
                session=live,
                before=before,
                before_id=before_id,
                limit=3,
                all_runs=False,
            )
            if not page:
                break
            keys = [(i.at, i.id) for i in page]
            assert keys == sorted(keys)
            seen += [(i.kind, i.id) for i in page]
            before, before_id = page[0].at, page[0].id
    assert len(seen) == 8
    assert len(set(seen)) == 8


def test_trigger_lines() -> None:
    def run(source: str, **ctx: Any) -> m.AgentRun:
        return m.AgentRun(source=source, context=ctx)

    assert trigger_of(run("cron")) == {"kind": "cron", "label": None}
    assert trigger_of(run("webhook")) == {"kind": "webhook", "label": None}
    assert trigger_of(run("delegation"), delegated_by="Copilot") == {
        "kind": "delegation",
        "label": "Copilot",
    }
    assert trigger_of(run("manual", originating_operator="bob"), started_by="Bob") == {
        "kind": "manual",
        "label": "Bob",
    }
    # A colleague's chat run names nobody's words, only who started it.
    assert trigger_of(
        run("chat", originating_operator="bob", task="User: SECRET"), started_by="Bob"
    ) == {
        "kind": "chat",
        "label": "Bob",
    }


async def test_a_run_a_colleague_started_by_hand_is_not_autonomous(
    app_session: AppSessionFactory,
) -> None:
    """POST /agents/{id}/run by Bob (source "manual", his operator, no chat
    session) is Bob's, not the agent's own: off Alice's timeline unless "All
    runs". Alice's own hand-started run is hers and stays on it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, _ = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        bobs = _run(tenant, agent, "manual", {"originating_operator": "bob", "task": "BOB TASK"})
        nobodys = _run(tenant, agent, "manual", {"task": "no operator"})
        hers = _run(tenant, agent, "manual", {"originating_operator": "alice"})
        # Delegated, but carrying a person: not autonomous either.
        carried = _run(tenant, agent, "delegation", {"originating_operator": "bob"})
        db.add_all([bobs, nobodys, hers, carried])
        await db.flush()
        plain = await live_timeline(
            db, member=alice, agent=agent, session=live, before=None, limit=50, all_runs=False
        )
        everything = await live_timeline(
            db, member=alice, agent=agent, session=live, before=None, limit=50, all_runs=True
        )
    assert _run_ids(plain) == {hers.id}
    assert _run_ids(everything) == {hers.id, bobs.id, nobodys.id, carried.id}


def test_autonomous_means_no_person_behind_the_run() -> None:
    def run(source: str, **ctx: Any) -> m.AgentRun:
        return m.AgentRun(source=source, context=ctx)

    for source in ("cron", "event", "webhook", "delegation", "decision", "handoff"):
        assert is_autonomous_run(run(source))
    assert not is_autonomous_run(run("manual"))
    assert not is_autonomous_run(run("chat"))
    assert not is_autonomous_run(run("cron", originating_operator="bob"))
    assert not is_autonomous_run(run("delegation", chat_session_id=str(uuid.uuid4())))
