"""Readers hear `chat.message_created` and may watch the run -- but only while
they still reach the session's agent, and they never write (Ruling 45)."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.chat.audience import extra_readers, session_audience_or_nobody, use_extra_readers
from oc8.chat.watch import may_watch_run
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat

pytestmark = pytest.mark.asyncio


async def test_reader_with_reach_is_told_and_may_watch_one_without_gets_nothing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        bob = await copilot_seat(db, tenant, "bob")
        carol = await copilot_seat(db, tenant, "carol", with_role=False)
        session = await db.get(m.ChatSession, alice.session_id)
        assert session is not None
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=session.agent_id,
            source="chat",
            context={"chat_session_id": str(session.id), "originating_operator": "alice"},
        )
        db.add(run)
        await db.flush()
        bob_row = await db.get(m.OrgMember, bob.member_id)
        carol_row = await db.get(m.OrgMember, carol.member_id)
        alice_row = await db.get(m.OrgMember, alice.member_id)
        assert alice_row is not None
        assert bob_row is not None and carol_row is not None

        async def readers(_db: object, _s: m.ChatSession) -> list[uuid.UUID]:
            return [bob.member_id, carol.member_id]

        assert not await may_watch_run(db, member=bob_row, run_id=run.id)
        with use_extra_readers(readers):
            assert await session_audience_or_nobody(db, session=session) == [
                alice.member_id,
                bob.member_id,
            ]
            # Ruling 47: no live token/tool/reasoning stream for a reader.
            assert not await may_watch_run(db, member=bob_row, run_id=run.id)
            assert await may_watch_run(db, member=alice_row, run_id=run.id)
            assert not await may_watch_run(db, member=carol_row, run_id=run.id)


async def test_a_failing_extension_leaves_the_owner_informed(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        session = await db.get(m.ChatSession, alice.session_id)
        assert session is not None

        async def boom(_db: object, _s: m.ChatSession) -> list[uuid.UUID]:
            raise RuntimeError("extension is down")

        with use_extra_readers(boom):
            assert await session_audience_or_nobody(db, session=session) == [alice.member_id]
            assert await extra_readers(db, session=session) == []
        # The transaction is still usable afterwards.
        assert await db.get(m.ChatSession, session.id) is not None
