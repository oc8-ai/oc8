"""Every Copilot user turn carries the agents that fit it and that the chat's
owner may run -- never a follow-up, never another agent's chat, never a block
when embeddings are down."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
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
            db, session=session, tenant_id=tenant, message=REQUEST, originating_operator="anna"
        )
        again = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=msg,
            originating_operator="anna",
            operator_role=None,
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
            db, session=session, tenant_id=tenant, message=REQUEST, originating_operator="anna"
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
            db, session=session, tenant_id=tenant, message=REQUEST, originating_operator="anna"
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
            db, session=session, tenant_id=tenant, message=REQUEST, originating_operator="anna"
        )
        assert run is not None
        assert BLOCK not in run.context["task"]
        assert router.calls == []
