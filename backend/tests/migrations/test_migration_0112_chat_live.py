"""0112 adds the Live session kind and the run_input queue."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_one_live_session_per_member_and_agent(app_session: AppSessionFactory) -> None:
    tenant, member, agent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.ChatSession(tenant_id=tenant, agent_id=agent, member_id=member, kind="live"))
        db.add(m.ChatSession(tenant_id=tenant, agent_id=agent, member_id=member))  # chat: fine
        await db.flush()
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(m.ChatSession(tenant_id=tenant, agent_id=agent, member_id=member, kind="live"))
            await db.flush()


async def test_a_new_session_is_an_ordinary_chat(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        row = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=uuid.uuid4())
        db.add(row)
        await db.flush()
        await db.refresh(row)
        assert row.kind == "chat"


async def test_run_input_round_trip(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        row = m.RunInput(
            tenant_id=tenant,
            run_id=uuid.uuid4(),
            author_member_id=uuid.uuid4(),
            text="use the EU address",
        )
        db.add(row)
        await db.flush()
        assert row.consumed_at is None and row.consumed_step is None


async def test_run_input_text_is_bounded(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(
                m.RunInput(
                    tenant_id=tenant,
                    run_id=uuid.uuid4(),
                    author_member_id=uuid.uuid4(),
                    text="x" * 4001,
                )
            )
            await db.flush()


async def test_run_input_is_tenant_isolated(app_session: AppSessionFactory) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    run_id = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.RunInput(tenant_id=tenant, run_id=run_id, author_member_id=uuid.uuid4(), text="hi")
        )
    async with app_session(other) as db:
        rows = (
            (await db.execute(sa.select(m.RunInput).where(m.RunInput.run_id == run_id)))
            .scalars()
            .all()
        )
        assert rows == []


async def test_unknown_kind_is_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(
                m.ChatSession(
                    tenant_id=tenant, agent_id=uuid.uuid4(), member_id=uuid.uuid4(), kind="room"
                )
            )
            await db.flush()
