from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import redis.asyncio as redis

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result
from oc8.models.knowledge import EMBED_DIM
from oc8.realtime.bus import channel_for
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _CompanyMemoryWriteRouter:
    """One company-tier memory_write tool call (which requires approval), then a
    final text with no tool calls."""

    def __init__(self) -> None:
        self._calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self._calls += 1
        if self._calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="memory_write",
                        arguments={"tier": "company", "content": "the client prefers email"},
                    )
                ],
                usage=Usage(tokens_in=5, tokens_out=5),
                stop_reason="tool_use",
                provider="fake",
                model="fake",
            )
        return CompletionResult(
            text="noted",
            tool_calls=[],
            usage=Usage(tokens_in=3, tokens_out=3),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _drain_for(pubsub, type_: str) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    for _ in range(30):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg is None:
            continue
        env: dict[str, Any] = json.loads(msg["data"])
        if env.get("type") == type_:
            return env
    return None


async def test_engine_hitl_approval_publishes_approval_created(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, redis_url: str
) -> None:
    # A fresh tenant (not ACME) so this test's own memory_write ApprovalRequest
    # never collides with the shared-ACME scalar_one() assertions elsewhere.
    tenant = uuid.uuid4()
    monkeypatch.setattr(
        "oc8.agent.engine.get_model_router", lambda: _CompanyMemoryWriteRouter()
    )
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())

    sub = redis.from_url(redis_url, decode_responses=True)
    pubsub = sub.pubsub()
    await pubsub.subscribe(channel_for(tenant))
    await pubsub.get_message(timeout=1.0)
    try:
        async with app_session(tenant) as db:
            dept = m.Department(
                tenant_id=tenant, name="Sales", frame={"memory": {"company": ["read"]}}
            )
            db.add(dept)
            await db.flush()
            agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Rep")
            db.add(agent)
            await db.flush()

            result = await run_agent(
                db, agent=agent, task_text="remember this", tenant_id=tenant
            )
            assert result.status == "waiting_for_approval"
            env = await _drain_for(pubsub, "approval.created")
        assert env is not None, "no approval.created event landed on the tenant channel"
        assert env["data"]["action_type"] == "memory_write"
        assert env["data"]["status"] == "pending"
        assert uuid.UUID(env["data"]["approval_id"])  # a real, populated id
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await sub.aclose()
