"""The gateway bills the prompt-cache split a provider reports: the Claude Code
runtime's calls go through here, so their cached prefix must reach the
usage record like an in-process run's does."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.main import create_app
from oc8.modelrouter.types import CompletionChunk, CompletionResult, Usage
from tests.api.test_llm_gateway import CHAT, _agent_run, _agent_token, _post, _result
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_CACHED = Usage(tokens_in=32100, tokens_out=7, cache_read_tokens=30000, cache_write_tokens=2000)


async def _record(app_session: AppSessionFactory, tenant: uuid.UUID) -> m.TokenUsageRecord:
    async with app_session(tenant) as db:
        return (
            await db.execute(
                select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)
            )
        ).scalar_one()


async def test_a_completion_bills_its_cached_tokens(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)

    async def fake_complete(*a: object, **kw: object) -> CompletionResult:
        return _result(usage=_CACHED)

    monkeypatch.setattr("oc8.api.llm_gateway.complete_with_fallback", fake_complete)
    code, body, _h = await _post(
        _agent_token(tenant, agent_id, run_id),
        {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert code == 200, body
    row = await _record(app_session, tenant)
    assert (row.tokens_in, row.cache_read_tokens, row.cache_write_tokens) == (32100, 30000, 2000)


async def test_a_stream_bills_its_cached_tokens(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)

    async def fake_stream(*a: object, **kw: object) -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(text="ok")
        yield CompletionChunk(stop_reason="stop", usage=_CACHED)

    monkeypatch.setattr("oc8.api.llm_gateway.stream_completion_with_fallback", fake_stream)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            async with c.stream(
                "POST",
                CHAT,
                json={
                    "model": "x",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
            ) as r:
                assert r.status_code == 200
                [ln async for ln in r.aiter_lines()]
    row = await _record(app_session, tenant)
    assert (row.tokens_in, row.cache_read_tokens, row.cache_write_tokens) == (32100, 30000, 2000)
