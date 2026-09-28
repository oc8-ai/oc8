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
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.harness.step_timing import start_step
from oc8.main import create_app
from tests.api.test_internal_agent import (
    _agent_token,
    _mcp_backed_run,
    _plain_agent_run,
    _post_step,
    _post_tool,
)
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


async def _order_spies(
    monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> list[str]:
    """Records "commit" / "publish" in the order they actually happen, so a
    fix-round regression (publishing before the commit that persists the
    timing) shows up as `["publish", "commit"]` instead of the required
    `["commit", "publish"]` -- the same ordering `publish_run_tool_call`'s own
    call site already gets right, which review round 1 found four of this
    endpoint's `publish_run_step_timing` call sites got backwards.
    """
    order: list[str] = []
    original_commit = AsyncSession.commit

    async def _spy_commit(self: AsyncSession, *a: Any, **kw: Any) -> None:
        await original_commit(self, *a, **kw)
        order.append("commit")

    async def _spy_publish(tenant_id: Any, *, run_id: Any, timing: dict[str, Any]) -> None:
        order.append("publish")
        published.append(timing)

    monkeypatch.setattr(AsyncSession, "commit", _spy_commit)
    monkeypatch.setattr("oc8.api.v1.internal_agent.publish_run_step_timing", _spy_publish)
    return order


async def test_the_suspend_path_publishes_only_after_its_commit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_dispatch_one_tool`'s suspend close (a clarification via ask_user):
    review round 1 found this fired `publish_run_step_timing` before the
    `db.commit()` that actually persists the closed step timing, with output
    shaping, spill persistence, and transcript building running in between --
    a request failing in that window would hand an open tab a live event for
    data that was never saved. Fixed to publish after the commit, matching
    `publish_run_tool_call`'s own call site a few lines below it.
    """
    published: list[dict[str, Any]] = []
    order = await _order_spies(monkeypatch, published)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(db, tenant, stepTimings=[start_step(1)])
        agent_id, run_id = agent.id, run.id

    # Fixture setup above commits its own row -- only the request itself
    # should count towards the order asserted below.
    order.clear()

    code, body = await _post_tool(
        tenant, agent_id, run_id, "ask_user", {"question": "Welches Konto soll ich nehmen?"}
    )
    assert code == 200, body
    assert body["status"] == "waiting_for_input"

    assert published, "the closed step timing was never published"
    assert "commit" in order
    assert order.index("commit") < order.index("publish"), order

    async with app_session(tenant) as db:
        stored = await db.get(m.AgentRun, run_id)
        assert stored is not None
        assert stored.context["stepTimings"][-1] == published[-1]


async def test_the_max_steps_early_exit_publishes_only_after_its_commit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`step()`'s max-steps early-exit branch: review round 2 found this was
    the one site exempted from round 1 as "already correct" -- it turned out
    to share the same defect, just with a two-line window instead of a loop
    or an intervening DB write. `_finish_step_timing` (now removed, it had
    exactly one caller) did `finish_step` then `publish_run_step_timing`
    BEFORE `run.context = ctx; await db.commit()`. Fixed the same way as the
    other four sites: publish only after that commit lands.
    """
    from oc8.config import get_settings

    published: list[dict[str, Any]] = []
    order = await _order_spies(monkeypatch, published)

    tenant = uuid.uuid4()
    max_steps = get_settings().agent_max_steps
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(
            db, tenant, steps=max_steps, stepTimings=[start_step(1)]
        )
        agent_id, run_id = agent.id, run.id

    # Fixture setup above commits its own row -- only the request itself
    # should count towards the order asserted below.
    order.clear()

    body = await _post_step(tenant, agent_id, run_id)
    assert body["done"] is True
    assert body["text"] == "Reached step limit."

    assert published, "the closed step timing was never published"
    assert "commit" in order
    assert order.index("commit") < order.index("publish"), order

    async with app_session(tenant) as db:
        stored = await db.get(m.AgentRun, run_id)
        assert stored is not None
        assert stored.context["stepTimings"][-1] == published[-1]


async def test_the_require_approval_path_publishes_only_after_its_commit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_dispatch_one_tool`'s REQUIRE_APPROVAL close: review round 1 found this
    fired `publish_run_step_timing` before the eventual `db.commit()`, with a
    `raise_approval` DB write running in between. Fixed the same way as the
    suspend path above.
    """
    published: list[dict[str, Any]] = []
    order = await _order_spies(monkeypatch, published)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _mcp_backed_run(
            db,
            tenant,
            destructive_tools=["create_record"],
            approval_templates={"create_record": "Allow deleting record {id}?"},
        )
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "stepTimings": [start_step(1)]}
        await db.flush()

    # Fixture setup above commits its own rows -- only the request itself
    # should count towards the order asserted below.
    order.clear()

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "create_record",
        {"id": 7, "justification": "Duplicate record"},
    )
    assert code == 200, body
    assert body["status"] == "waiting_for_approval"

    assert published, "the closed step timing was never published"
    assert "commit" in order
    assert order.index("commit") < order.index("publish"), order

    async with app_session(tenant) as db:
        stored = await db.get(m.AgentRun, run_id)
        assert stored is not None
        assert stored.context["stepTimings"][-1] == published[-1]
