"""A model turn the provider will not serve -- the organisation's monthly
token quota is used up, or the provider is throttling -- is not an internal
error. /step answers 429 with a sentence an operator can act on, and records
that sentence as the run's failed verdict, so the run does not end with
nothing but a non-zero container exit."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.main import create_app
from oc8.modelrouter.fallback import ProviderBudgetExceeded
from tests.api.test_internal_agent import _agent_token, _plain_agent_run
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

QUOTA_REASON = "The monthly token quota for openai_compatible is used up until 2026-11-01."
THROTTLE_REASON = "The model provider is rate-limiting requests; try again shortly."


def _quota_used_up() -> Exception:
    return ProviderBudgetExceeded("openai_compatible", dt.datetime(2026, 11, 1, tzinfo=dt.UTC))


def _throttled() -> Exception:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(429, request=request, text='{"error":"rate limited"}')
    return httpx.HTTPStatusError(
        "429 from https://provider.example/v1/chat/completions",
        request=request,
        response=response,
    )


def _failing_stream(exc: Exception) -> Any:
    async def _stream(*args: object, **kw: object) -> Any:
        raise exc
        yield  # pragma: no cover -- makes this an async generator

    return _stream


async def _step(tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> httpx.Response:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
            )


@pytest.mark.parametrize(
    ("raised", "reason"),
    [(_quota_used_up, QUOTA_REASON), (_throttled, THROTTLE_REASON)],
    ids=["quota", "throttled"],
)
async def test_a_turn_the_provider_will_not_serve_is_a_429_with_the_reason(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    raised: Any,
    reason: str,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id
    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _failing_stream(raised())
    )

    r = await _step(tenant, agent_id, run_id)

    assert r.status_code == 429, r.text
    assert r.json()["detail"] == reason
    async with app_session(tenant) as db:
        fresh = await db.get(m.AgentRun, run_id)
        assert fresh is not None
        assert fresh.context["isolated_result"] == {"status": "failed", "output": reason}


async def test_any_other_upstream_failure_still_propagates(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the two limits get a verdict of their own: a broken request keeps
    failing the way it did, with no verdict written on its behalf."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    bad_request = httpx.HTTPStatusError(
        "400 from upstream", request=request, response=httpx.Response(400, request=request)
    )
    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _failing_stream(bad_request)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await _step(tenant, agent_id, run_id)

    async with app_session(tenant) as db:
        fresh = await db.get(m.AgentRun, run_id)
        assert fresh is not None
        assert "isolated_result" not in fresh.context
