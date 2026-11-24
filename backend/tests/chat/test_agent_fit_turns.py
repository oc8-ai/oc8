"""Every Copilot user turn carries the agents that fit it and that the chat's
owner may run -- never a follow-up, never another agent's chat, never a block
when embeddings are down."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agent.engine import open_run_task
from oc8.chat.service import regenerate_reply, send_message
from tests.conftest import AppSessionFactory
from tests.copilot.test_agent_match import (
    REQUEST,
    _agent,
    _department,
    _DownRouter,
    _fresh_cache,  # noqa: F401  (autouse fixture)
    _person,
    _route,
    _Router,
    _text,
)

pytestmark = pytest.mark.asyncio

BLOCK = "Agents that fit this request"


async def _office(
    db: AsyncSession, tenant: uuid.UUID, *, copilot_chat: bool = True
) -> m.ChatSession:
    copilot = await get_or_create_assistant(db, tenant_id=tenant)
    sales = await _department(db, tenant, "Sales")
    anna = await _person(db, tenant, "anna", seats=[sales])
    sales_agent = await _agent(db, tenant, sales, "Sales agent", "Writes quotations")
    session = m.ChatSession(
        tenant_id=tenant,
        agent_id=copilot.id if copilot_chat else sales_agent.id,
        member_id=anna.id,
    )
    db.add(session)
    await db.flush()
    task = await open_run_task(
        db, agent=copilot if copilot_chat else sales_agent, task_text="chat", tenant_id=tenant
    )
    session.task_id = task.id
    await db.flush()
    return session


async def _owner(db: AsyncSession, session: m.ChatSession) -> m.OrgMember:
    owner = await db.get(m.OrgMember, session.member_id)
    assert owner is not None
    return owner


def _fits() -> _Router:
    return _Router({_text("Sales agent", "Writes quotations"): 0.9})


async def test_user_turn_task_text_contains_fit_block(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _route(monkeypatch, _fits())
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator="anna",
            acting_member=await _owner(db, session),
        )
        assert run is not None
        assert BLOCK in run.context["task"]
        assert "- Sales agent (Sales) — fit 0.90" in run.context["task"]


async def test_regenerated_turn_contains_fit_block(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _route(monkeypatch, _fits())
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        msg, _run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator="anna",
            acting_member=await _owner(db, session),
        )
        again = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=msg,
            originating_operator="anna",
            operator_role=None,
            acting_member=await _owner(db, session),
        )
        assert BLOCK in again.context["task"]


async def test_no_fit_block_when_embeddings_down(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _route(monkeypatch, _DownRouter())
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator="anna",
            acting_member=await _owner(db, session),
        )
        assert run is not None
        assert BLOCK not in run.context["task"]


async def test_no_fit_block_when_matching_raises(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()

    async def boom(*_: Any, **__: Any) -> list[Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr("oc8.chat.service.matching_agents", boom)
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator="anna",
            acting_member=await _owner(db, session),
        )
        assert run is not None
        assert BLOCK not in run.context["task"]


async def test_no_fit_block_for_followup_and_non_assistant_sessions(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    router = _fits()
    _route(monkeypatch, router)
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator=None,
            for_person="anna",
            chat_channel="telegram",
            chat_channel_external_id="42",
            role="followup",
        )
        assert run is not None
        assert BLOCK not in run.context["task"]
        assert router.calls == []

    tenant = uuid.uuid4()
    router = _fits()
    _route(monkeypatch, router)
    async with app_session(tenant) as db:
        session = await _office(db, tenant, copilot_chat=False)
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator="anna",
            acting_member=await _owner(db, session),
        )
        assert run is not None
        assert BLOCK not in run.context["task"]
        assert router.calls == []


async def test_a_turn_by_someone_other_than_the_owner_gets_no_block(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    router = _fits()
    _route(monkeypatch, router)
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        owner = await db.get(m.OrgMember, session.member_id)
        sales = (await db.execute(select(m.Department))).scalars().first()
        assert owner is not None and sales is not None
        boss = await _person(db, tenant, "boss", seats=[sales])

        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator="boss",
            acting_member=boss,
        )
        assert run is not None and BLOCK not in run.context["task"]
        assert router.calls == []

        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator="anna",
            acting_member=owner,
        )
        assert run is not None and BLOCK in run.context["task"]

        again = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=msg,
            originating_operator="boss",
            operator_role=None,
            acting_member=boss,
        )
        assert BLOCK not in again.context["task"]


async def test_messenger_turn_by_the_owner_gets_the_block(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _route(monkeypatch, _fits())
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator=None,
            for_person="anna",
            chat_channel="telegram",
            chat_channel_external_id="42",
        )
        assert run is not None and BLOCK in run.context["task"]


async def test_messenger_turn_for_another_subject_gets_no_block(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    router = _fits()
    _route(monkeypatch, router)
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator=None,
            for_person="mallory",
            chat_channel="telegram",
            chat_channel_external_id="42",
        )
        assert run is not None and BLOCK not in run.context["task"]
        assert router.calls == []


async def test_a_failing_sql_statement_in_matching_does_not_block_the_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()

    async def failing_sql(db: AsyncSession, **_: Any) -> list[Any]:
        await db.execute(text("SELECT 1/0"))
        return []

    monkeypatch.setattr("oc8.chat.service.matching_agents", failing_sql)
    async with app_session(tenant) as db:
        session = await _office(db, tenant)
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message=REQUEST,
            originating_operator="anna",
            acting_member=await _owner(db, session),
        )
        assert run is not None
        assert BLOCK not in run.context["task"]
