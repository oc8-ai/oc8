"""Task A4 (+ rulings C5/C8): every runtime executes a run against the agent
version pinned at intake, never against whatever the live `Agent` row says
at the moment of a given step or tool call.

The isolated control plane (`internal_agent._load`) re-read the live row on
every `/step` and `/tool`, the MCP gateway re-read `agent.narrowing` on every
`tools/list` and `tools/call`, and the in-process engine read the live row
once at start -- three runtimes, three different answers to "which
configuration is this run using". These tests pin the one answer.

The department frame is the deliberate exception: it is the tenant's policy
ceiling, and tightening it must bite immediately, mid-run included.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.agent.engine import _authorize, _max_steps, run_agent
from oc8.agents.versioning import publish_version
from oc8.api.v1.internal_agent import _load
from oc8.auth import get_identity_provider
from oc8.authz.pdp import Effect, effective_tool_policies
from oc8.main import create_app
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, Usage, chunk_from_result
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

CODING_FRAME: dict[str, Any] = {
    "tools": {"coding": {"enabled": True, "read": True, "modify": True, "approval_eur": None}}
}
CODING_READ_ONLY: dict[str, Any] = {
    "tools": {"coding": {"enabled": True, "read": True, "modify": False}}
}


class _FakeToolset:
    def __init__(self, call: Callable[[str, dict[str, Any]], Awaitable[str]]) -> None:
        self.tools = [NeutralTool(name="fs_write", description="write a file", parameters={})]
        self._call = call

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return await self._call(name, arguments)


class _ScriptedRouter:
    """One fs_write tool call, then a final answer. Records every request so a
    test can read the system prompt the engine actually sent."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.requests.append(req)
        if len(self.requests) == 1:
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id="c1", name="fs_write", arguments={"path": "/tmp/x"})],
                usage=Usage(tokens_in=1, tokens_out=1),
                stop_reason="tool_use",
                provider="fake",
                model="fake",
            )
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _pinned_run(
    db: Any, tenant: uuid.UUID, *, frame: dict[str, Any], **agent_fields: Any
) -> tuple[m.Agent, m.AgentRun]:
    """An agent published as v1 plus a running run pinned to v1 -- what
    `enqueue_run` produces, without needing the queue."""
    dept = m.Department(tenant_id=tenant, name="Eng", frame=frame)
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Coder", **agent_fields)
    db.add(agent)
    await db.flush()
    await publish_version(db, agent)
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        state=RunState.RUNNING.value,
        context={"task": "x"},
        agent_version_id=agent.current_version_id,
    )
    db.add(run)
    await db.flush()
    return agent, run


async def _ok(name: str, arguments: dict[str, Any]) -> str:
    return "wrote it"


# ------------------------------------------------------------ in-process engine


async def test_inprocess_run_uses_pinned_narrowing_after_midrun_publish(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _ScriptedRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run = await _pinned_run(db, tenant, frame=CODING_FRAME, narrowing={})
        # Published after the run was pinned: it must not reach this run.
        agent.narrowing = CODING_READ_ONLY
        await publish_version(db, agent)

        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            run_id=run.id,
            toolset=_FakeToolset(_ok),
        )

    assert result.tool_calls[-1]["result"] == "wrote it", result.tool_calls


async def test_inprocess_run_uses_pinned_mission_in_its_system_prompt(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _ScriptedRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run = await _pinned_run(db, tenant, frame=CODING_FRAME, mission="original mission")
        agent.mission = "changed mid-run"
        await publish_version(db, agent)

        await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            run_id=run.id,
            toolset=_FakeToolset(_ok),
        )

    system = " ".join(
        str(msg.content) for msg in router.requests[0].messages if msg.role == "system"
    )
    assert "original mission" in system
    assert "changed mid-run" not in system


async def test_department_frame_is_not_pinned(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tightening a department frame must take effect immediately, mid-run --
    the frame is the tenant's policy ceiling, not part of the agent's version."""
    router = _ScriptedRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run = await _pinned_run(db, tenant, frame=CODING_FRAME, narrowing={})
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = CODING_READ_ONLY
        await db.flush()

        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            run_id=run.id,
            toolset=_FakeToolset(_ok),
        )

    assert result.tool_calls[-1]["result"] != "wrote it", result.tool_calls


def test_engine_authorize_memory_write_uses_the_narrowing_it_is_given() -> None:
    frame = {"memory": {"department": ["read", "write"]}}
    agent = m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="A",
        # The live row forbids department writes...
        narrowing={"memory": {"department": ["read"]}},
    )
    tc = ToolCall(id="1", name="memory_write", arguments={"tier": "department", "content": "x"})
    kwargs: dict[str, Any] = dict(
        frame=frame,
        tool_policies=effective_tool_policies(frame, {}),
        connection_key=None,
        tool_scopes=None,
    )
    assert _authorize(agent, tc, **kwargs).effect is Effect.DENY
    # ...but the pinned version this run executes under allows them.
    assert _authorize(agent, tc, narrowing={}, **kwargs).effect is Effect.ALLOW


def test_engine_authorize_delegation_uses_the_team_lead_flag_it_is_given() -> None:
    agent = m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="A",
        is_team_lead=False,
    )
    tc = ToolCall(
        id="1", name="delegate_task", arguments={"task_text": "go", "agent_id": str(uuid.uuid4())}
    )
    kwargs: dict[str, Any] = dict(frame={}, tool_policies={}, connection_key=None, tool_scopes=None)
    assert _authorize(agent, tc, **kwargs).effect is Effect.DENY
    assert _authorize(agent, tc, is_team_lead=True, **kwargs).effect is Effect.ALLOW


def test_max_steps_reads_the_definition_it_is_given() -> None:
    assert _max_steps({"max_steps": 7}) == 7
    assert _max_steps({}) >= 1
    assert _max_steps(None) >= 1


# ------------------------------------------------------ isolated control plane


async def test_isolated_load_uses_pinned_config_not_live_row(
    app_session: AppSessionFactory,
) -> None:
    """Regression: internal_agent._load re-read the live Agent row on every
    /step and /tool, so a publish landed mid-run between two tool calls."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, run = await _pinned_run(db, tenant, frame={}, mission="original")
        agent.mission = "changed mid-run"
        await publish_version(db, agent)

        _agent, _dept, _conn, cfg = await _load(db, run)
    assert cfg["mission"] == "original"


# ------------------------------------------------------------- LLM gateway


async def test_llm_gateway_uses_the_pinned_model_config(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container's model calls go through the LLM gateway, one request per
    turn. Switching the agent's model mid-run must not switch the model a
    running run talks to."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first = m.ModelConfig(
            tenant_id=tenant, provider="openai_compatible", model="first-model", params={}
        )
        second = m.ModelConfig(
            tenant_id=tenant, provider="openai_compatible", model="second-model", params={}
        )
        db.add_all([first, second])
        await db.flush()
        agent, run = await _pinned_run(
            db, tenant, frame={}, status="running", model_config_id=first.id
        )
        agent.model_config_id = second.id
        await publish_version(db, agent)
        agent_id, run_id = agent.id, run.id

    seen: dict[str, Any] = {}

    async def fake_complete(*a: object, **kw: object) -> CompletionResult:
        seen["primary"] = kw.get("primary")
        return CompletionResult(
            text="ok",
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop",
            provider="openai_compatible",
            model="first-model",
        )

    monkeypatch.setattr("oc8.api.llm_gateway.complete_with_fallback", fake_complete)
    token = get_identity_provider().mint(
        tenant_id=tenant,
        subject=f"agent:{agent_id}",
        role="agent_default",
        kind="agent",
        scopes=[f"run:{run_id}"],
    )
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/llm/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text
    assert seen["primary"] is not None and seen["primary"].model == "first-model"
