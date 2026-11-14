"""The conversation's half of direct access: who a turn acts for, ACT while a
grant is active, and what deleting the chat takes with it."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.modes import ACT, MODES, mode_from_context, parse_command
from oc8.chat.service import delete_session, regenerate_reply, send_message
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import approved_offer, direct_office, grant

pytestmark = pytest.mark.asyncio


def test_act_is_resolvable_but_never_a_command() -> None:
    assert "act" not in MODES
    assert mode_from_context({"chat_mode": "act"}) is ACT
    assert parse_command("/act find it") == (None, "/act find it")


async def _session(db: AsyncSession, office_session: uuid.UUID) -> m.ChatSession:
    row = await db.get(m.ChatSession, office_session)
    assert row is not None
    return row


async def test_the_owners_web_turn_acts_for_them_and_runs_in_act_with_a_grant(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        session = await _session(db, office.session_id)
        msg, run = await send_message(
            db, session=session, tenant_id=tenant, message="find it", originating_operator="anna"
        )
        assert run is not None
        assert run.context["on_behalf_of"] == str(office.member_id)
        assert "chat_mode" not in run.context

        await grant(db, office)
        msg, run = await send_message(
            db, session=session, tenant_id=tenant, message="and now?", originating_operator="anna"
        )
        assert run is not None and msg.mode is None
        assert run.context["chat_mode"] == "act"
        assert "[Mode: act]" in run.context["task"]


async def test_a_typed_mode_wins_over_act(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        _msg, run = await send_message(
            db,
            session=await _session(db, office.session_id),
            tenant_id=tenant,
            message="/plan update it",
            originating_operator="anna",
        )
        assert run is not None and run.context["chat_mode"] == "plan"


async def test_someone_elses_words_carry_no_person(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        _msg, run = await send_message(
            db,
            session=await _session(db, office.session_id),
            tenant_id=tenant,
            message="look at this",
            originating_operator="admin",
        )
        assert run is not None
        assert "on_behalf_of" not in run.context
        assert run.context["acting_person_withheld"] == "someone_elses_words"


async def test_a_messenger_turn_carries_no_person(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        _msg, run = await send_message(
            db,
            session=await _session(db, office.session_id),
            tenant_id=tenant,
            message="find it",
            originating_operator=None,
            for_person="anna",
            chat_channel="telegram",
            chat_channel_external_id="42",
        )
        assert run is not None
        assert "on_behalf_of" not in run.context and "chat_mode" not in run.context


async def test_a_regenerated_reply_acts_for_the_owner_in_act(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        session = await _session(db, office.session_id)
        msg, _run = await send_message(
            db, session=session, tenant_id=tenant, message="find it", originating_operator="anna"
        )
        await grant(db, office)
        again = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=msg,
            originating_operator="anna",
            operator_role=None,
        )
        assert again.context["on_behalf_of"] == str(office.member_id)
        assert again.context["chat_mode"] == "act"
        other = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=msg,
            originating_operator="admin",
            operator_role=None,
        )
        assert "on_behalf_of" not in other.context
        assert other.context["acting_person_withheld"] == "someone_elses_words"


async def test_deleting_the_chat_takes_its_grants_and_open_offers(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        pending = await approved_offer(db, office, status="pending")
        await delete_session(db, tenant_id=tenant, session=await _session(db, office.session_id))
        await db.flush()
        left = (await db.execute(select(m.DirectAccessGrant))).scalars().all()
        assert left == []
        await db.refresh(pending)
        assert pending.status == "expired"
