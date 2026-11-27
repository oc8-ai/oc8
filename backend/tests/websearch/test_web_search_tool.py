"""The core tool `web_search` (spec §10): offered only on a turn that asked
for the web and only with a provider; refused under restricted material and
past six searches; its hits come back fenced and, once the answer cites
them, as chat sources of kind `web`; audited like every reading core tool on
the in-process engine and on the isolated runtime."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import (
    MAX_WEB_SEARCHES_PER_RUN,
    WEB_SEARCH,
    WEB_SEARCH_HITS_KEY,
    WEB_SEARCH_LIMIT,
    WEB_SEARCH_NOT_REQUESTED,
    WEB_SEARCH_RESTRICTED,
    WEB_SEARCH_UNAVAILABLE,
    ControlOutcome,
    execute_control_tool,
    offered_tools,
)
from oc8.authz.pdp import Decision, Effect
from oc8.chat.service import record_assistant_reply
from oc8.modelrouter import ToolCall
from oc8.websearch.service import offered, save_tenant_setting
from tests.audit.test_tool_call_audit import _tool_events
from tests.conftest import AppSessionFactory
from tests.websearch.conftest import FakeSearch, rows

pytestmark = pytest.mark.asyncio


async def _org(db: Any, tenant: uuid.UUID) -> None:
    db.add(
        m.Organization(id=tenant, slug=f"t{tenant.hex[:8]}", name="T", tier="standard", region="eu")
    )
    await db.flush()


async def _chat_run(
    db: Any, tenant: uuid.UUID, **context: Any
) -> tuple[m.Agent, m.Task, m.AgentRun]:
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Copilot",
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Chat",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        source="chat",
        state="running",
        context={"task": "go", **context},
    )
    db.add(run)
    await db.flush()
    return agent, task, run


async def _search(
    db: Any,
    tenant: uuid.UUID,
    agent: m.Agent,
    task: m.Task,
    run_id: uuid.UUID | None,
    query: str = "oc8 latest release",
) -> ControlOutcome:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=agent,
        task=task,
        tc=ToolCall(id="c1", name="web_search", arguments={"query": query, "max_results": 3}),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
        run_id=run_id,
    )
    assert outcome is not None
    return outcome


class _Stream:
    """The engine's model call, scripted, keeping the tool names of each request."""

    def __init__(self, script: list[Any]) -> None:
        from tests.agent.test_engine_parallel_reads import _ScriptedStream

        self._inner = _ScriptedStream(script)
        self.tools: list[set[str]] = []

    def __call__(self, *args: Any, **kw: Any) -> Any:
        self.tools.append({t.name for t in kw.get("tools") or []})
        return self._inner(*args, **kw)


# ------------------------------------------------------------------ offering


async def test_offered_only_when_the_turn_asked_and_a_provider_exists(
    app_session: AppSessionFactory, fake: FakeSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.config import get_settings

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        # No provider anywhere: not offered, even with Web on.
        assert not await offered(db, tenant_id=tenant, context={"web_search": True})
        monkeypatch.setattr(get_settings(), "web_search_provider", "searxng")
        monkeypatch.setattr(get_settings(), "web_search_base_url", fake.base)
        assert await offered(db, tenant_id=tenant, context={"web_search": True})
        # A trigger's or a delegated run's context does not set it.
        assert not await offered(db, tenant_id=tenant, context={"task": "check the inbox"})
        assert not await offered(db, tenant_id=tenant, context=None)
        assert not await offered(db, tenant_id=tenant, context={"web_search": "yes"})
        # The tenant switched it off: the platform default no longer applies.
        await save_tenant_setting(db, tenant_id=tenant, provider="off", base_url=None, api_key=None)
        assert not await offered(db, tenant_id=tenant, context={"web_search": True})


async def test_the_tool_list_carries_it_only_when_offered() -> None:
    agent = m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="A",
        definition={},
        presentation={},
    )
    plain = offered_tools(agent, assigned_skills=[], active_skills=[], mcp_tools=[])
    assert WEB_SEARCH.name not in {t.name for t in plain}
    on = offered_tools(
        agent, assigned_skills=[], active_skills=[], mcp_tools=[], offer_web_search=True
    )
    assert WEB_SEARCH.name in {t.name for t in on}


# ------------------------------------------------------------------ dispatch


async def test_hits_come_back_fenced_numbered_and_kept_on_the_run(
    app_session: AppSessionFactory, platform_searxng: FakeSearch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _chat_run(db, tenant, web_search=True)
        outcome = await _search(db, tenant, agent, task, run.id)
        stored = (await db.get(m.AgentRun, run.id)).context  # type: ignore[union-attr]
    assert not outcome.output.startswith("ERROR")
    # Inside the unforgeable data fence, never as text in oc8's own voice.
    assert '<tool_output nonce="' in outcome.output
    fenced = outcome.output.split('<tool_output nonce="', 1)[1]
    assert "[1] Hit 1\nhttps://example.org/1\nAbout 1" in fenced
    assert outcome.result_detail == {"provider": "searxng", "count": 3}
    assert platform_searxng.seen[0]["query"]["q"] == ["oc8 latest release"]
    assert stored["web_searches"] == 1
    assert [h["n"] for h in stored[WEB_SEARCH_HITS_KEY]] == [1, 2, 3]


async def test_a_second_search_keeps_numbers_and_continues_them(
    app_session: AppSessionFactory, platform_searxng: FakeSearch
) -> None:
    platform_searxng.answers = [
        (200, {"results": rows(2, "content")}),
        (
            200,
            {"results": [*rows(3, "content")[1:], {"title": "New", "url": "https://n.example/"}]},
        ),
    ]
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _chat_run(db, tenant, web_search=True)
        await _search(db, tenant, agent, task, run.id)
        second = await _search(db, tenant, agent, task, run.id, query="more")
        stored = (await db.get(m.AgentRun, run.id)).context  # type: ignore[union-attr]
    # Hit 2 keeps [2]; the new ones continue after it.
    assert "[2] Hit 2" in second.output and "[3] Hit 3" in second.output
    assert "[4] New" in second.output
    assert [h["url"] for h in stored[WEB_SEARCH_HITS_KEY]] == [
        "https://example.org/1",
        "https://example.org/2",
        "https://example.org/3",
        "https://n.example/",
    ]


async def test_refused_when_the_turn_did_not_ask_for_the_web(
    app_session: AppSessionFactory, platform_searxng: FakeSearch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _chat_run(db, tenant)
        outcome = await _search(db, tenant, agent, task, run.id)
        no_run = await _search(db, tenant, agent, task, None)
    assert outcome.output == WEB_SEARCH_NOT_REQUESTED
    assert no_run.output == WEB_SEARCH_NOT_REQUESTED
    assert platform_searxng.seen == []


async def test_refused_when_the_run_carries_restricted_material(
    app_session: AppSessionFactory, platform_searxng: FakeSearch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _chat_run(db, tenant, web_search=True, contains_restricted=True)
        outcome = await _search(db, tenant, agent, task, run.id)
    assert outcome.output == WEB_SEARCH_RESTRICTED
    assert platform_searxng.seen == []


async def test_at_most_six_searches_in_a_turn(
    app_session: AppSessionFactory, platform_searxng: FakeSearch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _chat_run(db, tenant, web_search=True)
        outcomes = [
            await _search(db, tenant, agent, task, run.id, query=f"q{i}")
            for i in range(MAX_WEB_SEARCHES_PER_RUN + 1)
        ]
    assert MAX_WEB_SEARCHES_PER_RUN == 6
    assert all(not o.output.startswith("ERROR") for o in outcomes[:6])
    assert outcomes[6].output == WEB_SEARCH_LIMIT
    assert len(platform_searxng.seen) == 6


async def test_no_provider_is_a_plain_tool_error(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        agent, task, run = await _chat_run(db, tenant, web_search=True)
        outcome = await _search(db, tenant, agent, task, run.id)
    assert outcome.output == WEB_SEARCH_UNAVAILABLE


async def test_a_provider_error_is_a_tool_error_the_model_can_explain(
    app_session: AppSessionFactory, platform_searxng: FakeSearch
) -> None:
    platform_searxng.answers = [(403, {"error": "forbidden"})]
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _chat_run(db, tenant, web_search=True)
        outcome = await _search(db, tenant, agent, task, run.id)
    assert outcome.output.startswith("ERROR: the web search failed: SearXNG answered HTTP 403")
    assert outcome.result_detail == {"provider": "searxng", "count": 0}


# ------------------------------------------------------------------ sources


async def test_cited_hits_become_web_sources_of_the_reply(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    hits = [
        {"n": i, "title": f"Hit {i}", "url": f"https://example.org/{i}", "snippet": f"About {i}"}
        for i in (1, 2, 3)
    ]
    async with app_session(tenant) as db:
        agent, _task, run = await _chat_run(db, tenant)
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
        db.add(session)
        await db.flush()
        run.context = {"chat_session_id": str(session.id), WEB_SEARCH_HITS_KEY: hits}
        await db.flush()
        await record_assistant_reply(
            db, run=run, output="Released in May [3], says the changelog [1]. See [2](x)."
        )
        reply = (
            await db.execute(select(m.ChatMessage).where(m.ChatMessage.session_id == session.id))
        ).scalar_one()
        kept = run.context.get(WEB_SEARCH_HITS_KEY)
    assert reply.parts == [
        {
            "type": "sources",
            "items": [
                {
                    "n": 1,
                    "title": "Hit 1",
                    "url": "https://example.org/1",
                    "favicon": None,
                    "snippet": "About 1",
                    "kind": "web",
                },
                {
                    "n": 3,
                    "title": "Hit 3",
                    "url": "https://example.org/3",
                    "favicon": None,
                    "snippet": "About 3",
                    "kind": "web",
                },
            ],
        }
    ]
    # Consumed: a later answer of this run does not inherit them.
    assert kept is None


# ------------------------------------------------------------------ runtimes + audit


async def test_the_engine_audits_one_call_and_one_result_without_content(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    platform_searxng: FakeSearch,
) -> None:
    """A Copilot chat with Web on: the engine offers the tool, runs it under
    the routine frame verdict like every reading core tool, and audits one
    `tool.call` (with the query) and one `tool.result` (count, provider,
    duration -- no hit)."""
    from oc8.agent.engine import run_agent
    from tests.agent.test_engine_parallel_reads import (
        _fixture,
        _RecordingServer,
        _session_factory,
        _turn,
    )

    recorder = _RecordingServer(delay_s=0.0)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _session_factory(recorder, ["read_a"]))
    script = _Stream(
        [
            _turn("", ToolCall(id="c1", name="web_search", arguments={"query": "oc8 release"})),
            _turn("It shipped in May [1]."),
        ]
    )
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", script)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _conn = await _fixture(db, tenant, tool_scopes={"read": ["read_a"]})
        agent.is_tenant_assistant = True
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
        db.add(session)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="running",
            source="chat",
            context={"task": "go", "chat_session_id": str(session.id), "web_search": True},
        )
        db.add(run)
        await db.flush()
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=None, run_id=run.id
        )
    assert result.status == "done"
    assert "web_search" in script.tools[0]
    assert len(platform_searxng.seen) == 1
    (call,) = await _tool_events(app_session, tenant, "tool.call:web_search")
    assert call.decision == "allow"
    assert call.resource["arguments"] == {"query": "oc8 release"}
    (event,) = await _tool_events(app_session, tenant, "tool.result:web_search")
    assert event.resource["outcome"] == "ok"
    assert event.resource["provider"] == "searxng"
    assert event.resource["count"] == 3
    assert "duration_ms" in event.resource
    assert "example.org" not in json.dumps(event.resource)
    assert "About 1" not in json.dumps(event.resource)


async def test_a_run_without_web_never_offers_it(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    platform_searxng: FakeSearch,
) -> None:
    """A trigger's run (no `web_search` in its context) has no web_search,
    even with a provider configured."""
    from oc8.agent.engine import run_agent
    from tests.agent.test_engine_parallel_reads import (
        _fixture,
        _RecordingServer,
        _session_factory,
        _turn,
    )

    recorder = _RecordingServer(delay_s=0.0)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _session_factory(recorder, ["read_a"]))
    script = _Stream([_turn("Nothing to do.")])
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", script)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes={"read": ["read_a"]})
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, state="running", source="cron", context={}
        )
        db.add(run)
        await db.flush()
        await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn, run_id=run.id
        )
    assert script.tools and "web_search" not in script.tools[0]


async def test_the_isolated_runtime_runs_and_audits_it_the_same_way(
    app_session: AppSessionFactory, platform_searxng: FakeSearch
) -> None:
    from tests.api.test_internal_agent import _plain_agent_run, _post_tool

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(db, tenant, web_search=True)
        agent.is_tenant_assistant = True
        await db.flush()
        agent_id, run_id = agent.id, run.id
    code, body = await _post_tool(tenant, agent_id, run_id, "web_search", {"query": "oc8"})
    assert code == 200, body
    assert body["status"] == "ok", body
    assert "Hit 1" in body["output"]
    (call,) = await _tool_events(app_session, tenant, "tool.call:web_search")
    assert call.decision == "allow"
    (event,) = await _tool_events(app_session, tenant, "tool.result:web_search")
    assert event.resource["provider"] == "searxng"
    assert event.resource["count"] == 3
    assert "example.org" not in json.dumps(event.resource)
