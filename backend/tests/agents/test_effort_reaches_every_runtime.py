"""The chat's level reaches the in-process engine, the isolated /step and the
LLM gateway with no per-runtime plumbing (spec 2026-11-11 §7, §10)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agents.versioning import publish_version
from oc8.main import create_app
from oc8.modelrouter import CompletionChunk, CompletionResult, Usage, chunk_from_result
from oc8.modelrouter.types import ModelParams
from oc8.runtime.states import RunState
from tests.agent.test_engine_parallel_reads import _fixture, _RecordingServer, _session_factory
from tests.api.test_internal_agent import _agent_token as _step_token
from tests.api.test_internal_agent import _plain_agent_run
from tests.api.test_llm_gateway import _agent_token as _gateway_token
from tests.api.test_llm_gateway import _post
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _RecordingStream:
    def __init__(self) -> None:
        self.params: list[ModelParams] = []

    def __call__(self, *args: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:
        self.params.append(kw["params"])

        async def _gen() -> AsyncIterator[CompletionChunk]:
            yield chunk_from_result(
                CompletionResult(
                    text="Done.",
                    tool_calls=[],
                    usage=Usage(1, 1),
                    stop_reason="stop",
                    provider="fake",
                    model="fake",
                )
            )

        return _gen()


async def test_the_in_process_engine(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    recorder = _RecordingStream()
    monkeypatch.setattr(
        "oc8.agent.mcp_client.McpSession", _session_factory(_RecordingServer(), ["read_a"])
    )
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", recorder)
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes={"read": ["read_a"], "modify": []})
        cfg = await db.get(m.ModelConfig, agent.model_config_id)
        assert cfg is not None
        cfg.supports_reasoning = True
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state=RunState.RUNNING.value,
            context={"task": "go", "effort_override": "thorough"},
        )
        db.add(run)
        await db.flush()
        await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, run_id=run.id, mcp_conn=conn
        )
    assert recorder.params and recorder.params[0].reasoning == "thorough"


async def test_an_auto_router_run_keeps_each_tiers_own_level_through_an_escalation(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control is hidden on Auto, so the chat's stored pick does not
    apply: a failed first call escalates to the strong tier, and both calls
    carry what the PINNED definition configures (effort high -> thorough,
    temperature 0.2), never the live draft's values nor the chat's fast."""
    tenant = uuid.uuid4()
    recorder = _RecordingStream()
    calls: list[ModelParams] = []

    models: list[str] = []

    def flaky(*args: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:
        models.append(kw["primary"].model)
        if not calls:
            calls.append(kw["params"])
            recorder.params.append(kw["params"])
            raise RuntimeError("provider down")
        calls.append(kw["params"])
        return recorder(*args, **kw)

    monkeypatch.setattr(
        "oc8.agent.mcp_client.McpSession", _session_factory(_RecordingServer(), ["read_a"])
    )
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", flaky)
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes={"read": ["read_a"], "modify": []})
        tiers = [
            m.ModelConfig(
                tenant_id=tenant,
                provider="ollama",
                model=name,
                locality="local",
                supports_reasoning=True,
                params={},
            )
            for name in ("fast-model", "balanced-model", "strong-model")
        ]
        db.add_all(tiers)
        await db.flush()
        auto = m.ModelConfig(
            tenant_id=tenant,
            provider="auto",
            model="router",
            locality="cloud",
            params={
                "tiers": {
                    "fast": str(tiers[0].id),
                    "balanced": str(tiers[1].id),
                    "strong": str(tiers[2].id),
                }
            },
        )
        db.add(auto)
        await db.flush()
        agent.model_config_id = auto.id
        agent.definition = {"model_params": {"effort": "high", "temperature": 0.2}}
        version = await publish_version(db, agent)
        # The live row is the operator's draft; a run must not read it.
        agent.definition = {"model_params": {"effort": "low", "temperature": 0.9}}
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            agent_version_id=version.id,
            state=RunState.RUNNING.value,
            context={"task": "go", "effort_override": "fast"},
        )
        db.add(run)
        await db.flush()
        await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, run_id=run.id, mcp_conn=conn
        )
    assert len(recorder.params) >= 2, "the first call failed and the escalated one ran"
    assert models[-1] == "strong-model", models
    for params in (recorder.params[0], recorder.params[-1]):
        assert (params.reasoning, params.effort) == ("thorough", "high")
        assert params.temperature == 0.2


async def test_the_isolated_step(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    recorder = _RecordingStream()
    monkeypatch.setattr("oc8.api.v1.internal_agent.stream_completion_with_fallback", recorder)
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(db, tenant, effort_override="fast")
        cfg = m.ModelConfig(
            tenant_id=tenant, provider="openai_compatible", model="o3", supports_reasoning=True
        )
        db.add(cfg)
        await db.flush()
        agent.model_config_id = cfg.id
        await db.flush()
        agent_id, run_id = agent.id, run.id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {_step_token(tenant, agent_id, run_id)}"},
            )
    assert r.status_code == 200, r.text
    assert recorder.params and recorder.params[0].reasoning == "fast"


async def _gateway_run(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, thinks: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        cfg = m.ModelConfig(
            tenant_id=tenant, provider="openai_compatible", model="o3", supports_reasoning=thinks
        )
        db.add(cfg)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            status="running",
            narrowing={},
            definition={},
            presentation={},
            model_config_id=cfg.id,
        )
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state=RunState.RUNNING.value,
            context={"task": "x", "effort_override": "thorough"},
        )
        db.add(run)
        await db.flush()
        return agent.id, run.id


@pytest.mark.parametrize(("thinks", "level"), [(True, "thorough"), (False, None)])
async def test_the_llm_gateway(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    thinks: bool,
    level: str | None,
) -> None:
    tenant = uuid.uuid4()
    seen: list[ModelParams] = []

    async def fake_complete(*a: object, **kw: Any) -> CompletionResult:
        seen.append(kw["params"])
        return CompletionResult(
            text="fertig",
            tool_calls=[],
            usage=Usage(1, 1),
            stop_reason="stop",
            provider="openai_compatible",
            model="o3",
        )

    monkeypatch.setattr("oc8.api.llm_gateway.complete_with_fallback", fake_complete)
    agent_id, run_id = await _gateway_run(app_session, tenant, thinks=thinks)
    code, body, _h = await _post(
        _gateway_token(tenant, agent_id, run_id),
        {"model": "whatever", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert code == 200, body
    assert seen[0].reasoning == level
