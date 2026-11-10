"""The Live timeline (spec §3.1): the member's own live turns merged at read
time with the agent's autonomous runs -- and, the privacy half, never a run
started from another member's chat unless a manager asks for "All runs", and
even then never that member's text."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.live import (
    LiveItem,
    get_or_create_live_session,
    is_autonomous_run,
    is_colleague_run,
    live_timeline,
    timeline_runs,
    trigger_names,
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
            db,
            member=alice,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=False,
            reaches_runs=True,
        )
        everything = await live_timeline(
            db,
            member=alice,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=True,
            reaches_runs=True,
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
            db,
            member=alice,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=False,
            reaches_runs=True,
        )
        everything = await live_timeline(
            db,
            member=alice,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=True,
            reaches_runs=True,
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
                reaches_runs=True,
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
            db,
            member=alice,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=True,
            reaches_runs=True,
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
            reaches_runs=True,
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
                reaches_runs=True,
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
    cron = m.Trigger(kind="cron", cron_expression="0 9 * * 1-5", task_text="x")
    event = m.Trigger(kind="event", event_source="sales", event_type="deal.won", task_text="x")
    hook = m.Trigger(kind="webhook", task_text="x")
    # An agent schedule is read in UTC unless the trigger names a zone.
    assert trigger_of(run("cron"), trigger=cron) == {
        "kind": "cron",
        "label": "0 9 * * 1-5",
        "timezone": "UTC",
    }
    berlin = m.Trigger(
        kind="cron", cron_expression="0 9 * * *", timezone="Europe/Berlin", task_text="x"
    )
    assert trigger_of(run("cron"), trigger=berlin)["timezone"] == "Europe/Berlin"
    assert trigger_of(run("event"), trigger=event) == {"kind": "event", "label": "sales/deal.won"}
    assert trigger_of(run("webhook"), trigger=hook) == {"kind": "webhook", "label": None}
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
            db,
            member=alice,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=False,
            reaches_runs=True,
        )
        everything = await live_timeline(
            db,
            member=alice,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=True,
            reaches_runs=True,
        )
    assert _run_ids(plain) == {hers.id}
    assert _run_ids(everything) == {hers.id, bobs.id, nobodys.id, carried.id}


async def test_autonomous_means_no_person_behind_the_run(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()

    def run(source: str, **ctx: Any) -> m.AgentRun:
        return m.AgentRun(tenant_id=tenant, source=source, context=ctx)

    async with app_session(tenant) as db:
        for source in ("cron", "event", "webhook", "delegation", "decision", "handoff"):
            assert await is_autonomous_run(db, run(source))
        assert not await is_autonomous_run(db, run("manual"))
        assert not await is_autonomous_run(db, run("chat"))
        assert not await is_autonomous_run(db, run("cron", originating_operator="bob"))
        assert not await is_autonomous_run(db, run("delegation", chat_session_id=str(uuid.uuid4())))


async def test_trigger_names_resolve_the_firing_trigger_and_stay_in_the_tenant(
    app_session: AppSessionFactory,
) -> None:
    """Schedules, events and webhooks are labelled from the Trigger row the
    run's `coalesce_key` (`trigger:<id>`) names -- read in one batch."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _, _ = await _world(db, tenant)
        cron = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="cron",
            task_text="x",
            cron_expression="0 9 * * *",
        )
        event = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="event",
            task_text="x",
            event_source="sales",
            event_type="deal.won",
        )
        hook = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="webhook",
            task_text="x",
            webhook_token=uuid.uuid4().hex,
        )
        db.add_all([cron, event, hook])
        await db.flush()
        runs = [
            _run(tenant, agent, t.kind, {}, coalesce_key=f"trigger:{t.id}")
            for t in (cron, event, hook)
        ]
        runs.append(_run(tenant, agent, "cron", {}, coalesce_key="trigger:not-a-uuid"))
        db.add_all(runs)
        await db.flush()
        names = await trigger_names(db, runs, tenant_id=tenant)
    assert [names[r.id]["label"] for r in runs] == ["0 9 * * *", "sales/deal.won", None, None]


async def test_python_and_sql_agree_on_who_is_behind_a_run(
    app_session: AppSessionFactory,
) -> None:
    """Any present value but null and "" names a person, in both checks --
    a stray `false` or `0` errs on the side of "somebody's"."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, _ = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        values: tuple[Any, ...] = (None, "", False, 0, "bob", {})
        runs = [
            _run(tenant, agent, "cron", {key: value})
            for key in ("originating_operator", "for_person", "chat_session_id")
            for value in values
        ]
        db.add_all(runs)
        await db.flush()
        shown = _run_ids(
            await live_timeline(
                db,
                member=alice,
                agent=agent,
                session=live,
                before=None,
                limit=100,
                all_runs=False,
                reaches_runs=True,
            )
        )
        for run in runs:
            assert (run.id in shown) == await is_autonomous_run(db, run), run.context


async def test_a_run_on_her_behalf_is_hers(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, alice, _ = await _world(db, tenant)
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
        )
        hers = _run(tenant, agent, "delegation", {"for_person": "alice"})
        bobs = _run(tenant, agent, "delegation", {"for_person": "bob"})
        db.add_all([hers, bobs])
        await db.flush()
        plain = await live_timeline(
            db,
            member=alice,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=False,
            reaches_runs=True,
        )
        assert _run_ids(plain) == {hers.id}
        assert not await is_colleague_run(db, hers, alice)
        assert await is_colleague_run(db, bobs, alice)


async def test_started_by_names_the_person_not_the_delegating_agent(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _, _ = await _world(db, tenant)
        task = m.Task(
            tenant_id=tenant,
            department_id=agent.department_id,
            assigned_agent_id=agent.id,
            title="t",
            state="in_progress",
        )
        db.add(task)
        await db.flush()
        wake = _run(
            tenant, agent, "delegation", {"for_person": "bob", "parent_task_id": str(task.id)}
        )
        db.add(wake)
        await db.flush()
        names = await trigger_names(db, [wake], tenant_id=tenant)
    assert names[wake.id] == {"kind": "delegation", "label": "A", "started_by": "Bob"}


async def test_the_several_people_marker_is_never_a_member(
    app_session: AppSessionFactory,
) -> None:
    """A member whose subject happens to equal the marker owns nothing: a
    mixed-owner run stays a colleague's run for him too -- off his default
    timeline and not watchable live."""
    from oc8.chat.watch import may_watch_run
    from oc8.runtime.person import SEVERAL_PEOPLE

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _, _ = await _world(db, tenant)
        mallory = m.OrgMember(tenant_id=tenant, subject=SEVERAL_PEOPLE, subject_uuid=uuid.uuid4())
        db.add(mallory)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=mallory.id,
                department_id=agent.department_id,
                seat_role="dept_viewer",
            )
        )
        mixed = _run(tenant, agent, "delegation", {"for_person": SEVERAL_PEOPLE})
        db.add(mixed)
        await db.flush()
        live = await get_or_create_live_session(
            db, tenant_id=tenant, member_id=mallory.id, agent_id=agent.id
        )
        plain = await live_timeline(
            db,
            member=mallory,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=False,
            reaches_runs=True,
        )
        everything = await live_timeline(
            db,
            member=mallory,
            agent=agent,
            session=live,
            before=None,
            limit=50,
            all_runs=True,
            reaches_runs=True,
        )
        assert await is_colleague_run(db, mixed, mallory)
        assert _run_ids(plain) == set()
        assert _run_ids(everything) == {mixed.id}
        assert not await may_watch_run(db, member=mallory, run_id=mixed.id)


def test_her_runs_outside_chat_test_the_source_column_before_any_json() -> None:
    """PostgreSQL evaluates an AND's arguments in order; the `context->>`
    tests detoast every run's context, so the cheap column goes first."""
    from oc8.chat.live import _hers_outside_chat

    member = m.OrgMember(tenant_id=uuid.uuid4(), subject="alice")
    sql = str(_hers_outside_chat(member).compile(dialect=postgresql.dialect()))
    assert sql.index("agent_run.source") < sql.index("agent_run.context")


async def test_the_live_timeline_walks_the_agent_created_index_and_drops_chat_runs_by_column(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _alice, bob = await _world(db, tenant)
        stmt = (
            timeline_runs(agent=agent, member=bob, all_runs=False, reaches_runs=True)
            .order_by(m.AgentRun.created_at.desc(), m.AgentRun.id.desc())
            .limit(51)
        )
        sql = str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(row[0] for row in (await db.execute(text(f"EXPLAIN {sql}"))).all())
    assert "ix_agent_run_agent_created" in plan
    # Not-all-runs never reads a chat run's context: the column test is a
    # top-level filter, ahead of the JSON arms.
    assert "Filter: ((source <> 'chat'::text)" in plan
