"""The isolated runtime's own live-publish wiring for step timings (run step
timeline plan, addendum to Task 3).

dev's own capture (`oc8.agent.harness.step_timing`, wired into this file's
`/step` and `/tool` handlers already) is not rebuilt here -- see
`tests/runtime/test_isolated_runtime.py::test_step_timings_reach_the_run_result`
for the existing proof that it reaches `RunResult`. This file proves the one
genuinely missing piece the addendum adds: a live `run.step_timing` WS event
fired the moment each of those handlers' existing `finish_step` calls closes
a step, mirroring the parity check
`test_the_internal_endpoints_step_streams_token_deltas_live` already runs for
`run.token_delta`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.main import create_app
from tests.api.test_internal_agent import _agent_token, _plain_agent_run
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _SpyBus:
    def __init__(self, sink: list[tuple[str, dict[str, Any]]]) -> None:
        self._sink = sink

    async def publish_event(
        self, tenant_id: Any, type_: str, data: dict[str, Any], **kw: Any
    ) -> None:
        self._sink.append((type_, data))


async def test_step_publishes_a_live_step_timing_when_the_step_finishes(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.modelrouter.types import CompletionChunk, Usage

    published: list[tuple[str, dict[str, Any]]] = []

    # Same module-level-import scoping hazard test_internal_agent.py's own
    # live-publish tests document: publish_run_step_timing resolves
    # get_event_bus via emit.py's own module-level import.
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _SpyBus(published))
    monkeypatch.setattr("oc8.realtime.emit.get_event_bus", lambda: _SpyBus(published))

    async def fake_stream(*args: object, **kw: object) -> Any:
        yield CompletionChunk(text="done", provider="openai_compatible", model="opaas_ai:odoo-gpt")
        yield CompletionChunk(usage=Usage(tokens_in=5, tokens_out=2), stop_reason="stop")

    monkeypatch.setattr("oc8.api.v1.internal_agent.stream_completion_with_fallback", fake_stream)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    token = _agent_token(tenant, agent_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

    timing_events = [data for type_, data in published if type_ == "run.step_timing"]
    assert len(timing_events) == 1
    assert timing_events[0]["run_id"] == str(run_id)
    timing = timing_events[0]["timing"]
    assert timing["step"] == 1
    assert set(timing) == {"step", "model_wait_ms", "ttft_ms", "tool_wait_ms", "step_wall_ms"}

    async with app_session(tenant) as db:
        stored = await db.get(m.AgentRun, run_id)
        assert stored is not None
        assert stored.context["stepTimings"] == [timing]
