"""The read-only chat port enforces session access, and the extra-readers hook
it rests on admits only valid, same-tenant, reachable members (Ruling 45)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.audience import is_reader_only, session_audience, use_extra_readers
from oc8.chat.port import get_messages, list_sessions, search_candidates, session_access
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat

pytestmark = pytest.mark.asyncio


def _sharing(session_id: uuid.UUID, *readers: uuid.UUID):  # type: ignore[no-untyped-def]
    async def fn(_db: AsyncSession, session: m.ChatSession) -> list[uuid.UUID]:
        return list(readers) if session.id == session_id else []

    return fn


async def _member(db: AsyncSession, member_id: uuid.UUID) -> m.OrgMember:
    row = await db.get(m.OrgMember, member_id)
    assert row is not None
    return row


async def test_port_only_returns_what_the_member_may_open(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice_seat = await copilot_seat(db, tenant, "alice")
        bob_seat = await copilot_seat(db, tenant, "bob")
        alice = await _member(db, alice_seat.member_id)
        theirs = await db.get(m.ChatSession, bob_seat.session_id)
        mine = await db.get(m.ChatSession, alice_seat.session_id)
        assert theirs is not None and mine is not None
        mine.title, theirs.title = "Mine", "Bob's"
        db.add(
            m.ChatMessage(tenant_id=tenant, session_id=theirs.id, role="user", content="private")
        )
        await db.flush()

        assert [s.title for s in await list_sessions(db, member=alice)] == ["Mine"]
        assert await get_messages(db, member=alice, session_id=theirs.id) is None
        assert await get_messages(db, member=alice, session_id=uuid.uuid4()) is None
        assert await session_access(db, member=alice, session=theirs) is None
        assert [s.title for s in await search_candidates(db, member=alice)] == ["Mine"]

        with use_extra_readers(_sharing(theirs.id, alice.id)):
            assert await session_access(db, member=alice, session=theirs) == "reader"
            msgs = await get_messages(db, member=alice, session_id=theirs.id)
            assert msgs is not None and msgs[0].content == "private"
            found = {s.title: s.access for s in await search_candidates(db, member=alice)}
            assert found == {"Mine": "owner", "Bob's": "reader"}
            # A reader's own list stays their own.
            assert [s.title for s in await list_sessions(db, member=alice)] == ["Mine"]
        assert await get_messages(db, member=alice, session_id=theirs.id) is None


async def test_community_default_audience_is_the_owner_only(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        session = await db.get(m.ChatSession, alice.session_id)
        assert session is not None
        assert await session_audience(db, session=session) == [alice.member_id]
        assert not await is_reader_only(db, session=session, member_id=uuid.uuid4())


async def test_extension_reader_ids_are_filtered(app_session: AppSessionFactory) -> None:
    tenant, other_tenant = uuid.uuid4(), uuid.uuid4()
    async with app_session(other_tenant) as odb:
        stranger = await copilot_seat(odb, other_tenant, "stranger")
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        ok = await copilot_seat(db, tenant, "ok")
        gone = await copilot_seat(db, tenant, "gone")
        gone_row = await _member(db, gone.member_id)
        gone_row.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        session = await db.get(m.ChatSession, alice.session_id)
        assert session is not None
        fn = _sharing(
            session.id,
            ok.member_id,
            ok.member_id,  # duplicate
            alice.member_id,  # the owner herself
            gone.member_id,  # offboarded
            stranger.member_id,  # other tenant
            uuid.uuid4(),  # does not exist
        )
        with use_extra_readers(fn):
            assert await session_audience(db, session=session) == [alice.member_id, ok.member_id]
            assert await is_reader_only(db, session=session, member_id=ok.member_id)
            assert not await is_reader_only(db, session=session, member_id=gone.member_id)
            assert not await is_reader_only(db, session=session, member_id=alice.member_id)
            gone_member = await _member(db, gone.member_id)
            assert await session_access(db, member=gone_member, session=session) is None


async def test_a_reader_without_agent_reach_is_not_in_the_audience(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        bob = await copilot_seat(db, tenant, "bob", with_role=False)  # no copilot:use
        bob_row = await _member(db, bob.member_id)
        session = await db.get(m.ChatSession, alice.session_id)
        assert session is not None
        with use_extra_readers(_sharing(session.id, bob.member_id)):
            assert await session_audience(db, session=session) == [alice.member_id]
            assert await session_access(db, member=bob_row, session=session) is None
            assert await get_messages(db, member=bob_row, session_id=session.id) is None


async def test_search_candidates_is_capped(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        alice_row = await _member(db, alice.member_id)
        for i in range(4):
            db.add(
                m.ChatSession(
                    tenant_id=tenant, agent_id=uuid.uuid4(), member_id=alice.member_id, title=str(i)
                )
            )
        await db.flush()
        assert len(await search_candidates(db, member=alice_row, limit=2)) == 2
        assert len(await search_candidates(db, member=alice_row, limit=0)) == 1
        assert len(await search_candidates(db, member=alice_row, limit=10_000)) == 5
