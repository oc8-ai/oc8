from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from oc8 import models as m
from oc8.chat.service import record_assistant_reply, send_message
from oc8.realtime.emit import publish_after_commit
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat

pytestmark = pytest.mark.asyncio


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[uuid.UUID], str, dict[str, Any]]] = []

    async def publish_member_event(  # type: ignore[no-untyped-def]
        self, tenant_id, member_ids, type_, data, *, source
    ):
        if member_ids:  # the real bus publishes nothing for an empty audience
            self.calls.append((list(member_ids), type_, data))

    async def publish_event(self, *args: Any, **kwargs: Any) -> None:
        return None


async def test_user_and_assistant_turns_are_announced_to_the_owner_only(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    rec = _Recorder()
    with (
        patch("oc8.realtime.emit.get_event_bus", return_value=rec),
        patch("oc8.chat.service.get_event_bus", return_value=rec),
        patch("oc8.runtime.intake.publish_run"),
    ):
        async with app_session(tenant) as db:
            seat = await copilot_seat(db, tenant, "alice")
            await copilot_seat(db, tenant, "bob")
            session = await db.get(m.ChatSession, seat.session_id)
            assert session is not None
            _user_msg, run = await send_message(
                db,
                session=session,
                tenant_id=tenant,
                message="hello",
                originating_operator="alice",
                author_member_id=seat.member_id,
            )
            assert run is not None
            run_id = run.id
        await asyncio.sleep(0)
        async with app_session(tenant) as db:
            loaded = await db.get(m.AgentRun, run_id)
            assert loaded is not None
            await record_assistant_reply(db, run=loaded, output="hi")
            assert not any(c[2]["role"] == "assistant" for c in rec.calls), (
                "published before commit"
            )
        await asyncio.sleep(0.05)
    roles = [(c[1], c[2]["role"]) for c in rec.calls]
    assert ("chat.message_created", "user") in roles
    assert ("chat.message_created", "assistant") in roles
    for audience, _type, data in rec.calls:
        assert audience == [seat.member_id]
        assert set(data) == {"session_id", "message_id", "role", "run_id"}
    user_event = next(c for c in rec.calls if c[2]["role"] == "user")
    assert user_event[2]["run_id"] == str(run_id)


async def test_session_owner_who_lost_agent_access_gets_no_chat_message_created(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Binding ruling U2/4: the audience re-checks the agent, so an owner whose
    copilot:use was taken away is not told about new turns any more."""
    tenant = uuid.uuid4()
    rec = _Recorder()
    with (
        patch("oc8.realtime.emit.get_event_bus", return_value=rec),
        patch("oc8.chat.service.get_event_bus", return_value=rec),
        patch("oc8.runtime.intake.publish_run"),
    ):
        async with app_session(tenant) as db:
            seat = await copilot_seat(db, tenant, "alice")
            session = await db.get(m.ChatSession, seat.session_id)
            assert session is not None
            _user_msg, run = await send_message(
                db,
                session=session,
                tenant_id=tenant,
                message="hello",
                originating_operator="alice",
                author_member_id=seat.member_id,
            )
            assert run is not None
            run_id = run.id
        assert rec.calls, "the owner was told while she could still reach the agent"
        rec.calls.clear()
        async with app_session(tenant) as db:
            owner = await db.get(m.OrgMember, seat.member_id)
            assert owner is not None
            owner.role_id = None
        async with app_session(tenant) as db:
            loaded = await db.get(m.AgentRun, run_id)
            assert loaded is not None
            await record_assistant_reply(db, run=loaded, output="hi")
        await asyncio.sleep(0.05)
    assert rec.calls == []


async def test_publish_after_commit_is_dropped_on_rollback(
    app_session: AppSessionFactory,
) -> None:
    fired: list[str] = []

    async def _publish() -> None:
        fired.append("x")

    async with app_session(uuid.uuid4()) as db:
        publish_after_commit(db, _publish)
        await db.rollback()
        await db.commit()
    await asyncio.sleep(0.01)
    assert fired == []

    async with app_session(uuid.uuid4()) as db:
        publish_after_commit(db, _publish)
    await asyncio.sleep(0.01)
    assert fired == ["x"]


async def test_a_savepoint_rollback_keeps_the_pending_event(
    app_session: AppSessionFactory,
) -> None:
    """Only the ROOT transaction's rollback drops pending events: a SAVEPOINT
    rolled back inside a transaction that then commits must not lose them."""
    fired: list[str] = []

    async def _publish() -> None:
        fired.append("x")

    async with app_session(uuid.uuid4()) as db:
        publish_after_commit(db, _publish)
        savepoint = await db.begin_nested()
        await savepoint.rollback()
    await asyncio.sleep(0.01)
    assert fired == ["x"]


async def test_an_audience_lookup_failure_never_breaks_the_reply(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Realtime never breaks a domain write: the reply is recorded, the event
    is skipped."""
    tenant = uuid.uuid4()
    rec = _Recorder()

    async def _boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("audience lookup failed")

    with (
        patch("oc8.realtime.emit.get_event_bus", return_value=rec),
        patch("oc8.chat.service.get_event_bus", return_value=rec),
        patch("oc8.runtime.intake.publish_run"),
        patch("oc8.chat.audience.member_can_reach_agent", _boom),
    ):
        async with app_session(tenant) as db:
            seat = await copilot_seat(db, tenant, "alice")
            session = await db.get(m.ChatSession, seat.session_id)
            assert session is not None
            _user_msg, run = await send_message(
                db,
                session=session,
                tenant_id=tenant,
                message="hello",
                originating_operator="alice",
                author_member_id=seat.member_id,
            )
            assert run is not None
            run_id = run.id
        async with app_session(tenant) as db:
            loaded = await db.get(m.AgentRun, run_id)
            assert loaded is not None
            await record_assistant_reply(db, run=loaded, output="hi")
        await asyncio.sleep(0.05)
    assert rec.calls == []
    async with app_session(tenant) as db:
        from sqlalchemy import select

        replies = (
            (
                await db.execute(
                    select(m.ChatMessage).where(
                        m.ChatMessage.session_id == seat.session_id,
                        m.ChatMessage.role == "assistant",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.content for r in replies] == ["hi"]
