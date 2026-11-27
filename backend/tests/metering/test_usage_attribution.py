"""Every path that bills a model call names the person it was spent for, so the
Cost Center can chart usage per member: the chat's owner for chat upkeep
(checkpoints, titles), the person a run acts for (`resolve_acting_person`) for
runs, in process, isolated and through the LLM gateway. A run that acts for
nobody, or whose person cannot be resolved, is billed without one -- never
not billed."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.chat import checkpoint, titles
from oc8.connections.acting import ACTING_PERSON_WITHHELD, NO_ACTING_PERSON
from oc8.metering.usage import usage_member_id
from oc8.modelrouter.types import CompletionChunk, CompletionResult, Usage
from tests.agent.test_engine_direct_access import _copilot, _patch, _run
from tests.agent.test_engine_parallel_reads import _RecordingServer, _turn
from tests.agents.test_department_cache_wiring import (
    _CountingRouter,
    _seed_department_with_two_agents,
)
from tests.api.test_internal_agent import _as_stream, _mcp_backed_run, _post_tool
from tests.api.test_llm_gateway import CHAT, _agent_run, _agent_token
from tests.chat.test_auto_titles import _FakeComplete as _TitleComplete
from tests.chat.test_auto_titles import _result as _title_result
from tests.chat.test_checkpoint import _alternating
from tests.chat.test_checkpoint import _FakeComplete as _SummaryComplete
from tests.chat.test_message_tree import _session
from tests.chat.test_replay import _chain
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import chat_run, direct_office
from tests.copilot.helpers import copilot_seat

pytestmark = pytest.mark.asyncio


async def _members(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[uuid.UUID | None]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.TokenUsageRecord.member_id)
                    .where(m.TokenUsageRecord.tenant_id == tenant)
                    .order_by(m.TokenUsageRecord.ts)
                )
            ).scalars()
        )


async def _cache_hits(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[uuid.UUID | None]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.TokenUsageRecord.member_id).where(
                        m.TokenUsageRecord.tenant_id == tenant,
                        m.TokenUsageRecord.cache_hit.is_(True),
                    )
                )
            ).scalars()
        )


async def _person(db: Any, tenant: uuid.UUID, subject: str = "alice") -> uuid.UUID:
    member = m.OrgMember(
        tenant_id=tenant, all_departments=True, subject=subject, subject_uuid=uuid.uuid4()
    )
    db.add(member)
    await db.flush()
    return member.id


async def _run_for(db: Any, tenant: uuid.UUID, agent: m.Agent, subject: str) -> uuid.UUID:
    """A run a person started by hand: it acts for them (`originating_operator`)."""
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        state="running",
        context={"originating_operator": subject, "task": "classify this ticket"},
    )
    db.add(run)
    await db.flush()
    return run.id


# ----------------------------------------------------------------- chat upkeep


async def test_checkpoint_usage_names_the_chat_owner(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", _SummaryComplete())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        owner = session.member_id
        rows = await _chain(db, session, _alternating(10, 200))
        made = await checkpoint.make_checkpoint(
            db, session=session, path=rows, window=1000, model_locality="local"
        )
        assert made is not None
    assert await _members(app_session, tenant) == [owner]


async def test_title_usage_names_the_chat_owner(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seat = await copilot_seat(db, tenant, "alice")
    with patch("oc8.chat.titles.complete_with_fallback", _TitleComplete(_title_result("Budget"))):
        async with app_session(tenant) as db:
            await titles.generate_auto_title(
                db, tenant_id=tenant, session_id=seat.session_id, message="our budget"
            )
    assert await _members(app_session, tenant) == [seat.member_id]


# ------------------------------------------------------------ in-process engine


async def test_engine_usage_names_the_acting_person(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.engine.get_model_router", _CountingRouter)
    tenant = uuid.uuid4()
    _dept, agent, _other = await _seed_department_with_two_agents(
        app_session, tenant, caching_enabled=False
    )
    async with app_session(tenant) as db:
        alice = await _person(db, tenant)
        run_id = await _run_for(db, tenant, agent, "alice")
    async with app_session(tenant) as db:
        await run_agent(
            db, agent=agent, task_text="classify this ticket", tenant_id=tenant, run_id=run_id
        )
    assert await _members(app_session, tenant) == [alice]


async def test_engine_cache_hit_usage_names_the_acting_person(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second identical request is served from the department cache; the
    zero-token hit row still names who it was served for -- here someone else
    than the person whose run filled the cache."""
    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    _dept, agent_a, agent_b = await _seed_department_with_two_agents(app_session, tenant)
    async with app_session(tenant) as db:
        alice = await _person(db, tenant, "alice")
        bob = await _person(db, tenant, "bob")
        run_a = await _run_for(db, tenant, agent_a, "alice")
        run_b = await _run_for(db, tenant, agent_b, "bob")
    for agent, run_id in ((agent_a, run_a), (agent_b, run_b)):
        async with app_session(tenant) as db:
            await run_agent(
                db, agent=agent, task_text="classify this ticket", tenant_id=tenant, run_id=run_id
            )
    assert router.calls == 1, "the second run must be a cache hit"
    assert await _cache_hits(app_session, tenant) == [bob]
    assert sorted(map(str, await _members(app_session, tenant))) == sorted([str(alice), str(bob)])


async def test_engine_usage_without_a_person_stays_null(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.engine.get_model_router", _CountingRouter)
    tenant = uuid.uuid4()
    _dept, agent, _other = await _seed_department_with_two_agents(
        app_session, tenant, caching_enabled=False
    )
    async with app_session(tenant) as db:
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, state="running", context={"task": "x"}
        )
        db.add(run)
        await db.flush()
        run_id = run.id
    async with app_session(tenant) as db:
        await run_agent(
            db, agent=agent, task_text="classify this ticket", tenant_id=tenant, run_id=run_id
        )
    assert await _members(app_session, tenant) == [None]


async def test_engine_usage_is_billed_when_the_person_cannot_be_resolved(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attribution is best-effort: a lookup whose SQL fails leaves the usage
    without a person. It never fails the run, loses the bill or poisons the
    run's transaction -- the failure stays inside its savepoint."""

    async def _broken(db: Any, _run: Any) -> Any:
        await db.execute(text("SELECT * FROM no_such_table_for_attribution"))
        raise AssertionError("unreachable")

    monkeypatch.setattr("oc8.agent.engine.get_model_router", _CountingRouter)
    monkeypatch.setattr("oc8.metering.usage.resolve_acting_person", _broken)
    tenant = uuid.uuid4()
    _dept, agent, _other = await _seed_department_with_two_agents(
        app_session, tenant, caching_enabled=False
    )
    async with app_session(tenant) as db:
        await _person(db, tenant)
        run_id = await _run_for(db, tenant, agent, "alice")
    async with app_session(tenant) as db:
        result = await run_agent(
            db, agent=agent, task_text="classify this ticket", tenant_id=tenant, run_id=run_id
        )
        # The run's own transaction is still usable after the failed lookup.
        assert (await db.execute(select(m.AgentRun.id).where(m.AgentRun.id == run_id))).scalar_one()
    assert result.output
    assert await _members(app_session, tenant) == [None]


async def test_a_failing_flush_is_not_swallowed_as_a_missing_person(
    app_session: AppSessionFactory,
) -> None:
    """Only the lookup is best-effort. The caller's own pending writes flush
    outside it: a broken one raises where it happened, instead of being
    reported as "no person" and surfacing later as a PendingRollbackError."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member_id = await _person(db, tenant)
        run = m.AgentRun(
            tenant_id=tenant, agent_id=uuid.uuid4(), state="running", context={"task": "x"}
        )
        db.add(
            m.OrgMember(
                id=member_id,
                tenant_id=tenant,
                all_departments=True,
                subject="clash",
                subject_uuid=uuid.uuid4(),
            )
        )
        with pytest.raises(IntegrityError):
            await usage_member_id(db, run)
        await db.rollback()


async def test_a_live_copilot_turn_is_billed_to_its_member(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live web turn the conversation's member wrote (the direct-access run)
    books its completions to that member."""
    _patch(monkeypatch, _RecordingServer(delay_s=0), [_turn("done")])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        await _run(db, office, agent, run)
    assert await _members(app_session, tenant) == [office.member_id]


async def test_a_run_whose_person_is_withheld_is_billed_to_nobody(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run started from another one that must act for nobody keeps that
    privacy in the bill: no member, even though a starter's name travels on."""
    monkeypatch.setattr("oc8.agent.engine.get_model_router", _CountingRouter)
    tenant = uuid.uuid4()
    _dept, agent, _other = await _seed_department_with_two_agents(
        app_session, tenant, caching_enabled=False
    )
    async with app_session(tenant) as db:
        await _person(db, tenant)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="running",
            context={
                "task": "classify this ticket",
                "originating_operator": "alice",
                ACTING_PERSON_WITHHELD: NO_ACTING_PERSON,
            },
        )
        db.add(run)
        await db.flush()
        run_id = run.id
    async with app_session(tenant) as db:
        await run_agent(
            db, agent=agent, task_text="classify this ticket", tenant_id=tenant, run_id=run_id
        )
    assert await _members(app_session, tenant) == [None]


# ------------------------------------------------------------ isolated runtime


def _completion(text: str = "fertig") -> CompletionResult:
    return CompletionResult(
        text=text,
        tool_calls=[],
        usage=Usage(tokens_in=120, tokens_out=30),
        stop_reason="stop",
        provider="openai_compatible",
        model="opaas_ai:odoo-gpt",
    )


async def test_isolated_step_usage_names_the_acting_person(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    async def fake_complete(*_a: object, **_kw: object) -> CompletionResult:
        return _completion()

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            status="running",
            narrowing={},
            definition={},
        )
        db.add(agent)
        await db.flush()
        alice = await _person(db, tenant)
        run_id = await _run_for(db, tenant, agent, "alice")
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
            )
            assert r.status_code == 200, r.text
    assert await _members(app_session, tenant) == [alice]


async def test_isolated_tool_usage_names_the_acting_person(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An autonomous agent's irreversible call is first checked by a
    clarification model call on the tool endpoint; that call is billed to the
    person the run acts for."""

    class _Session:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            return '{"id": 7}'

    async def fake_complete(*_a: object, **_kw: object) -> CompletionResult:
        return _completion("NONE")

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _Session)
    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _mcp_backed_run(db, tenant, create_tools=["create_record"])
        agent = await db.get(m.Agent, agent_id)
        run = await db.get(m.AgentRun, run_id)
        assert agent is not None and run is not None
        conn = await db.get(m.McpConnection, uuid.UUID(run.context["mcp_connection_id"]))
        assert conn is not None
        conn.config = {**conn.config, "irreversible_tools": ["create_record"]}
        agent.definition = {"autonomy": "autonomous"}
        run.context = {**run.context, "originating_operator": "alice"}
        alice = await _person(db, tenant)

    code, result = await _post_tool(
        tenant, agent_id, run_id, "create_record", {"model": "crm.lead", "values": {}}
    )
    assert code == 200, result
    assert await _members(app_session, tenant) == [alice]


# ------------------------------------------------------------------ LLM gateway


async def test_gateway_usage_names_the_acting_person(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A streamed completion is billed by a detached task after the request
    session is gone: the person must already travel on the caller."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    async def fake_stream(*_a: object, **_kw: object) -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(text="fertig")
        yield CompletionChunk(stop_reason="stop", usage=Usage(tokens_in=9, tokens_out=5))

    monkeypatch.setattr("oc8.api.llm_gateway.stream_completion_with_fallback", fake_stream)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "originating_operator": "alice"}
        alice = await _person(db, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                CHAT,
                json={
                    "model": "gpt-4o",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
            )
            assert r.status_code == 200, r.text
    for _ in range(40):
        members = await _members(app_session, tenant)
        if members:
            break
        await asyncio.sleep(0.05)
    assert members == [alice]
