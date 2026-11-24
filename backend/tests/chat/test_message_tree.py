"""Where every chat message lands in its session's tree, and the transcript a
run is given (spec §5.3): only the path the person is looking at."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import Engine, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.chat.carry_over import _Door, _stage_turn
from oc8.chat.service import (
    create_session,
    record_answer_turn,
    record_assistant_reply,
    regenerate_reply,
    send_message,
)
from oc8.chat.tree import visible_path
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _session(
    db: object, tenant: uuid.UUID, *, kind: str = "chat", assistant: bool = False
) -> m.ChatSession:
    if assistant:
        agent = await get_or_create_assistant(db, tenant_id=tenant)  # type: ignore[arg-type]
    else:
        dept = m.Department(tenant_id=tenant, name="Vertrieb")
        db.add(dept)  # type: ignore[attr-defined]
        await db.flush()  # type: ignore[attr-defined]
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", status="running")
        db.add(agent)  # type: ignore[attr-defined]
        await db.flush()  # type: ignore[attr-defined]
    member = m.OrgMember(
        tenant_id=tenant, all_departments=True, subject="op", subject_uuid=uuid.uuid4()
    )
    db.add(member)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    session = await create_session(db, tenant_id=tenant, agent_id=agent.id, member_id=member.id)  # type: ignore[arg-type]
    session.kind = kind
    await db.flush()  # type: ignore[attr-defined]
    return session


async def _send(
    db: object, session: m.ChatSession, text: str, **kw: object
) -> tuple[m.ChatMessage, m.AgentRun]:
    msg, run = await send_message(
        db,  # type: ignore[arg-type]
        session=session,
        tenant_id=session.tenant_id,
        message=text,
        originating_operator="op",
        **kw,  # type: ignore[arg-type]
    )
    assert run is not None
    return msg, run


async def _commit(db: AsyncSession, tenant: uuid.UUID) -> None:
    """Commit as the executor does, then re-bind the transaction-local RLS
    tenant the commit dropped."""
    await db.commit()
    await db.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant)})


async def _reply(db: object, run: m.AgentRun, output: str) -> m.ChatMessage:
    await record_assistant_reply(db, run=run, output=output)  # type: ignore[arg-type]
    await _commit(db, run.tenant_id)  # type: ignore[arg-type]
    return (
        await db.execute(  # type: ignore[attr-defined]
            select(m.ChatMessage)
            .where(m.ChatMessage.run_id == run.id, m.ChatMessage.role == "assistant")
            .order_by(m.ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()


async def _leaf(db: object, session: m.ChatSession) -> uuid.UUID | None:
    return (
        await db.execute(  # type: ignore[attr-defined]
            select(m.ChatSession.current_leaf_id).where(m.ChatSession.id == session.id)
        )
    ).scalar_one()


async def _parent(db: object, message_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(  # type: ignore[attr-defined]
            select(m.ChatMessage.parent_id).where(m.ChatMessage.id == message_id)
        )
    ).scalar_one()


async def test_send_appends_to_leaf(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        u1, run1 = await _send(db, session, "first question")
        assert await _parent(db, u1.id) is None
        assert await _leaf(db, session) == u1.id
        r1 = await _reply(db, run1, "first answer")
        assert await _parent(db, r1.id) == u1.id
        assert await _leaf(db, session) == r1.id
        u2, _run2 = await _send(db, session, "second question")
        assert await _parent(db, u2.id) == r1.id
        assert await _leaf(db, session) == u2.id


async def test_transcript_after_regenerate_has_only_active_variant(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        u1, run1 = await _send(db, session, "question")
        r1 = await _reply(db, run1, "OLD-VARIANT")
        run_regen = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=u1,
            originating_operator="op",
            operator_role=None,
        )
        r1b = await _reply(db, run_regen, "NEW-VARIANT")
        assert await _parent(db, r1b.id) == await _parent(db, r1.id) == u1.id
        assert await _leaf(db, session) == r1b.id
        _u2, run2 = await _send(db, session, "follow on")
    task = str(run2.context["task"])
    assert "NEW-VARIANT" in task
    assert "OLD-VARIANT" not in task, "a hidden variant must not reach the next turn"


async def test_parked_answer_then_reply_stays_on_path(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        u, run = await _send(db, session, "book a room")
        answer = await record_answer_turn(
            db,
            run=run,
            question="Which floor?",
            pending=None,
            structured=None,
            text="the third",
            author_member_id=session.member_id,
        )
        assert answer is not None
        await _commit(db, tenant)
        assert await _parent(db, answer.id) == u.id
        assert await _leaf(db, session) == answer.id
        reply = await _reply(db, run, "booked on the third floor")
        assert await _parent(db, reply.id) == answer.id
        assert await _leaf(db, session) == reply.id
        await db.refresh(session)
        path = await visible_path(db, session=session)
    assert [msg.id for msg in path] == [u.id, answer.id, reply.id]


async def test_late_reply_does_not_move_leaf(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        r1 = await _reply(db, run1, "answer one")
        u2, run2 = await _send(db, session, "two")
        # The person edits "two" while its run is still going: a sibling of it
        # becomes the branch they read.
        edited = m.ChatMessage(
            tenant_id=tenant,
            session_id=session.id,
            role="user",
            content="two, edited",
            parent_id=r1.id,
        )
        db.add(edited)
        await db.flush()
        session.current_leaf_id = edited.id
        await _commit(db, tenant)
        late = await _reply(db, run2, "LATE answer two")
        assert await _parent(db, late.id) == u2.id, "the reply stays under its own turn"
        assert await _leaf(db, session) == edited.id, "the person stays on their branch"
        u3, run3 = await _send(db, session, "three")
        assert await _parent(db, u3.id) == edited.id
    assert "LATE answer two" not in str(run3.context["task"])


async def test_followup_appends_to_current_leaf(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "watch the invoices")
        r1 = await _reply(db, run1, "I will")
        f, _run_f = await _send(
            db,
            session,
            "Follow-up for invoices",
            role="followup",
            followup={"responsibility_id": str(uuid.uuid4())},
        )
        assert await _parent(db, f.id) == r1.id
        assert await _leaf(db, session) == f.id


async def test_quiet_followup_delete_reparents_and_moves_leaf(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "watch the invoices")
        r1 = await _reply(db, run1, "I will")
        followup = {"responsibility_id": str(uuid.uuid4())}
        f1, run_f1 = await _send(db, session, "Follow-up one", role="followup", followup=followup)
        # The person writes while the follow-up runs: their turn hangs under it.
        u2, _run2 = await _send(db, session, "meanwhile")
        assert await _parent(db, u2.id) == f1.id
        run_f1.state = "done"
        await record_assistant_reply(db, run=run_f1, output="")
        await _commit(db, tenant)
        assert await db.get(m.ChatMessage, f1.id) is None
        assert await _parent(db, u2.id) == r1.id, "children move up to the deleted row's parent"
        assert await _leaf(db, session) == u2.id
        f2, run_f2 = await _send(db, session, "Follow-up two", role="followup", followup=followup)
        assert await _leaf(db, session) == f2.id
        run_f2.state = "done"
        await record_assistant_reply(db, run=run_f2, output="")
        await _commit(db, tenant)
        assert await _leaf(db, session) == u2.id, "a deleted leaf hands over to its parent"


async def test_carried_over_turn_follows_its_runs_anchor(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        u1, run1 = await _send(db, session, "one")
        r1 = await _reply(db, run1, "answer one")
        agent = await db.get(m.Agent, session.agent_id)
        assert agent is not None
        await _stage_turn(
            db,
            session=session,
            member_id=session.member_id,
            agent=agent,
            door=_Door(
                originating_operator="op",
                for_person=None,
                chat_channel=None,
                chat_channel_external_id=None,
            ),
            text="and also this",
            anchor_id=u1.id,
        )
        await _commit(db, tenant)
        staged = (
            await db.execute(select(m.ChatMessage).where(m.ChatMessage.content == "and also this"))
        ).scalar_one()
        assert await _parent(db, staged.id) == r1.id
        assert await _leaf(db, session) == staged.id


async def test_live_session_stays_linear(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant, kind="live")
        u1, run1 = await _send(db, session, "LIVE-ONE")
        r1 = await _reply(db, run1, "LIVE-ANSWER")
        u2, run2 = await _send(db, session, "LIVE-TWO")
        for msg in (u1, r1, u2):
            assert await _parent(db, msg.id) is None
        assert await _leaf(db, session) is None
    task = str(run2.context["task"])
    assert "LIVE-ONE" in task and "LIVE-ANSWER" in task


async def test_regenerate_after_switching_branch_does_not_move_leaf(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        r1 = await _reply(db, run1, "answer one")
        u2, run2 = await _send(db, session, "two")
        r2 = await _reply(db, run2, "answer two")
        run_regen = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=u2,
            originating_operator="op",
            operator_role=None,
        )
        # Meanwhile the person edits "two": a sibling of it becomes their branch.
        edited = m.ChatMessage(
            tenant_id=tenant,
            session_id=session.id,
            role="user",
            content="two, edited",
            parent_id=r1.id,
        )
        db.add(edited)
        await db.flush()
        session.current_leaf_id = edited.id
        await _commit(db, tenant)
        again = await _reply(db, run_regen, "answer two, again")
        assert await _parent(db, again.id) == await _parent(db, r2.id) == u2.id
        assert await _leaf(db, session) == edited.id, "the person stays on their branch"


async def test_regenerate_transcript_ends_at_the_answered_question(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        u, run = await _send(db, session, "BOOK-A-ROOM")
        answer = await record_answer_turn(
            db,
            run=run,
            question="Which floor?",
            pending=None,
            structured=None,
            text="THE-THIRD-FLOOR",
            author_member_id=session.member_id,
        )
        assert answer is not None
        await _commit(db, tenant)
        first = await _reply(db, run, "FIRST-BOOKING")
        assert await _parent(db, first.id) == answer.id
        run_regen = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=u,
            originating_operator="op",
            operator_role=None,
        )
        again = await _reply(db, run_regen, "SECOND-BOOKING")
        assert await _parent(db, again.id) == answer.id
    task = str(run_regen.context["task"])
    assert "BOOK-A-ROOM" in task
    assert task.index("BOOK-A-ROOM") < task.index("THE-THIRD-FLOOR")
    assert "FIRST-BOOKING" not in task
    assert task.splitlines()[0].startswith("User: BOOK-A-ROOM")
    lines = task.splitlines()
    assert lines[1].startswith("User: ") and lines[1].endswith("THE-THIRD-FLOOR"), (
        "the answered question is the newest turn"
    )


async def test_secret_refusal_follows_the_redacted_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant, assistant=True)
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="my password is hunter2",
            originating_operator="op",
        )
        assert run is None
        refusal = (
            await db.execute(
                select(m.ChatMessage).where(
                    m.ChatMessage.session_id == session.id, m.ChatMessage.role == "assistant"
                )
            )
        ).scalar_one()
        assert await _parent(db, msg.id) is None
        assert await _parent(db, refusal.id) == msg.id
        assert await _leaf(db, session) == refusal.id


async def test_carried_over_turn_with_a_vanished_anchor_appends_to_the_leaf(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        r1 = await _reply(db, run1, "answer one")
        agent = await db.get(m.Agent, session.agent_id)
        assert agent is not None
        await _stage_turn(
            db,
            session=session,
            member_id=session.member_id,
            agent=agent,
            door=_Door(
                originating_operator="op",
                for_person=None,
                chat_channel=None,
                chat_channel_external_id=None,
            ),
            text="orphaned steer",
            anchor_id=uuid.uuid4(),
        )
        await _commit(db, tenant)
        staged = (
            await db.execute(select(m.ChatMessage).where(m.ChatMessage.content == "orphaned steer"))
        ).scalar_one()
        assert await _parent(db, staged.id) == r1.id
        assert await _leaf(db, session) == staged.id


_SESSION_ROW = re.compile(r"(FROM chat_session\b.*FOR UPDATE|UPDATE chat_session\b)", re.S)


@contextmanager
def statements() -> Iterator[list[str]]:
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_args: Any) -> None:
        seen.append(f"{statement} -- {parameters!r}")

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _record)


def is_audit_lock(sql: str, tenant: uuid.UUID) -> bool:
    """The tenant's audit lock -- told apart from the other advisory locks
    (a run's inputs, a conversation's offers) by its key, the tenant id."""
    return "pg_advisory_xact_lock" in sql and str(tenant) in sql


def locks_session_row(sql: str) -> bool:
    return bool(_SESSION_ROW.search(sql))


def audit_lock_comes_first(seen: list[str], tenant: uuid.UUID) -> None:
    """The tenant's audit lock is taken before the chat_session row is first
    locked or changed (`chat.tree`'s lock order)."""
    session_row = next(i for i, sql in enumerate(seen) if locks_session_row(sql))
    audit = next((i for i, sql in enumerate(seen) if is_audit_lock(sql, tenant)), None)
    assert audit is not None and audit < session_row, seen[session_row]


async def test_send_takes_the_audit_lock_before_the_session_row(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """A first turn in a sticky mode changes the session row twice (its mode,
    its task) -- after the audit lock, the order a run's end keeps."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant, assistant=True)
        await _commit(db, tenant)
        with statements() as seen:
            msg, _run = await _send(db, session, "/agent build me an invoice checker")
        assert session.mode == "agent" and msg.mode == "agent"
    audit_lock_comes_first(seen, tenant)


async def test_regenerate_door_takes_the_audit_lock_before_the_session_row(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    from unittest.mock import patch

    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.auth import get_identity_provider
    from oc8.main import create_app

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        agent_id = agent.id
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = (
                await c.post(
                    "/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=headers
                )
            ).json()["id"]
            with patch("oc8.runtime.intake.publish_run"):
                turn = (
                    await c.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={"message": "draft a reply"},
                        headers=headers,
                    )
                ).json()
            async with app_session(tenant) as db:
                run = await db.get(m.AgentRun, uuid.UUID(turn["runId"]))
                assert run is not None
                run.state = "done"
                await record_assistant_reply(db, run=run, output="first answer")
            with statements() as seen, patch("oc8.runtime.intake.publish_run"):
                again = await c.post(
                    f"/api/v1/chat/sessions/{sid}/messages/{turn['id']}/regenerate",
                    headers=headers,
                )
            assert again.status_code == 201, again.text
    audit_lock_comes_first(seen, tenant)
