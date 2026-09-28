"""oc8 as an OpenAI-compatible LLM gateway for agent runtimes (§8.7 R1).

An agent container gets no provider key. Instead it talks to this endpoint with
its own run-scoped token, and the control plane forwards -- keeping the model
router, BYOK, EU locality, metering and the §15.4 budget in the path. Like
pgbouncer: the client believes it speaks to the server.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.modelrouter.types import CompletionChunk, CompletionResult, Usage
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

CHAT = "/llm/v1/chat/completions"


async def _agent_run(
    db: Any, tenant: uuid.UUID, *, model: str = "opaas_ai:odoo-gpt"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    cfg = m.ModelConfig(tenant_id=tenant, provider="openai_compatible", model=model, params={})
    db.add(cfg)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Nora", status="running",
        narrowing={}, definition={}, presentation={}, model_config_id=cfg.id,
    )
    db.add(agent)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant, agent_id=agent.id, state=RunState.RUNNING.value,
        context={"task": "verkauf etwas"},
    )
    db.add(run)
    await db.flush()
    return agent.id, run.id, dept.id


def _agent_token(tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return get_identity_provider().mint(
        tenant_id=tenant, subject=f"agent:{agent_id}", role="agent_default",
        kind="agent", scopes=[f"run:{run_id}"],
    )


async def _post(token: str | None, body: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
    app = create_app()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(CHAT, json=body, headers=headers)
            try:
                payload = r.json() if r.content else None
            except ValueError:
                payload = r.text
            return r.status_code, payload, dict(r.headers)


def _result(text: str = "fertig", **kw: Any) -> CompletionResult:
    return CompletionResult(
        text=text, tool_calls=kw.get("tool_calls", []),
        usage=kw.get("usage", Usage(tokens_in=120, tokens_out=30)),
        stop_reason=kw.get("stop_reason", "stop"),
        provider="openai_compatible", model=kw.get("model", "opaas_ai:odoo-gpt"),
    )


# --------------------------------------------------------------------- auth


async def test_a_request_without_a_token_is_refused(app_session: AppSessionFactory) -> None:
    code, _body, _h = await _post(None, {"model": "gpt-4o", "messages": []})
    assert code in (401, 403)


async def test_an_operator_token_may_not_use_the_gateway(
    app_session: AppSessionFactory,
) -> None:
    """Only an agent shell drives a run through here. An operator token with the
    right shape must not become a way to spend a tenant's model budget."""
    tenant = uuid.uuid4()
    op = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    code, _body, _h = await _post(
        op, {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert code == 403


# ------------------------------------------------------------ non-streaming


async def test_a_completion_is_forwarded_and_metered(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func, select

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, dept_id = await _agent_run(db, tenant)

    async def fake_complete(*a: object, **kw: object) -> CompletionResult:
        return _result()

    monkeypatch.setattr("oc8.api.llm_gateway.complete_with_fallback", fake_complete)

    code, body, _h = await _post(
        _agent_token(tenant, agent_id, run_id),
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert code == 200, body
    assert body["choices"][0]["message"]["content"] == "fertig"
    assert body["object"] == "chat.completion"
    assert body["usage"]["prompt_tokens"] == 120
    assert body["usage"]["completion_tokens"] == 30

    async with app_session(tenant) as db:
        row = (
            await db.execute(
                select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)
            )
        ).scalar_one()
        # Attributed to the agent AND its department, so a department budget sees it.
        assert row.agent_id == agent_id and row.department_id == dept_id
        assert row.tokens_in == 120 and row.tokens_out == 30
        count = (
            await db.execute(
                select(func.count()).select_from(m.TokenUsageRecord).where(
                    m.TokenUsageRecord.tenant_id == tenant
                )
            )
        ).scalar_one()
        assert count == 1, "one call bills exactly once"


async def test_the_requested_model_is_ignored_in_favour_of_the_agents_config(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 1: the router decides (§9.2/9.4). A runtime must not be able to
    spend a tenant's money on a model the operator did not choose -- and the
    response echoes what actually ran, so a harness can log the truth."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant, model="opaas_ai:odoo-gpt")

    seen: dict[str, Any] = {}

    async def fake_complete(*a: object, **kw: object) -> CompletionResult:
        seen["primary"] = kw.get("primary")
        seen["no_config_model"] = kw.get("no_config_model")
        return _result()

    monkeypatch.setattr("oc8.api.llm_gateway.complete_with_fallback", fake_complete)

    code, body, _h = await _post(
        _agent_token(tenant, agent_id, run_id),
        {"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert code == 200, body
    primary = seen["primary"]
    assert primary is not None and primary.model == "opaas_ai:odoo-gpt"
    assert body["model"] == "opaas_ai:odoo-gpt", "the resolved model is echoed, not the request"


async def test_tools_and_tool_calls_round_trip(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.modelrouter.types import ToolCall

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)

    seen: dict[str, Any] = {}

    async def fake_complete(*a: object, **kw: object) -> CompletionResult:
        seen["tools"] = kw.get("tools")
        return _result(
            text="", tool_calls=[ToolCall(id="c1", name="create_record", arguments={"m": 1})],
            stop_reason="tool_use",
        )

    monkeypatch.setattr("oc8.api.llm_gateway.complete_with_fallback", fake_complete)

    code, body, _h = await _post(
        _agent_token(tenant, agent_id, run_id),
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "leg was an"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "create_record",
                        "description": "create it",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        },
    )
    assert code == 200, body
    tools = seen["tools"]
    assert [t.name for t in tools] == ["create_record"]
    call = body["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "create_record"
    assert json.loads(call["function"]["arguments"]) == {"m": 1}
    assert body["choices"][0]["finish_reason"] == "tool_calls"


# ----------------------------------------------------------------- policy


async def test_a_tenant_over_its_hard_budget_gets_429_and_no_provider_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget must bite BEFORE the spend, and it must be an error a harness
    already knows how to back off from -- not a 200 with an error string, which a
    harness would loop on."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, dept_id = await _agent_run(db, tenant)
        db.add(m.Budget(tenant_id=tenant, department_id=None, hard_limit_tokens=100))
        db.add(
            m.TokenUsageRecord(
                tenant_id=tenant, request_id=uuid.uuid4(), model="m", provider="p",
                tokens_in=500, tokens_out=500, agent_id=agent_id, department_id=dept_id,
            )
        )

    called = False

    async def fake_complete(*a: object, **kw: object) -> CompletionResult:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr("oc8.api.llm_gateway.complete_with_fallback", fake_complete)

    code, body, headers = await _post(
        _agent_token(tenant, agent_id, run_id),
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert code == 429, body
    assert "retry-after" in {k.lower() for k in headers}
    assert called is False, "not a single token may be spent over the hard limit"


# --------------------------------------------------------------- streaming


async def test_streaming_emits_sse_and_meters_at_the_end(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)

    async def fake_stream(*a: object, **kw: object) -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(text="Hal")
        yield CompletionChunk(text="lo")
        yield CompletionChunk(stop_reason="stop", usage=Usage(tokens_in=11, tokens_out=2))

    monkeypatch.setattr("oc8.api.llm_gateway.stream_completion_with_fallback", fake_stream)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            async with c.stream(
                "POST", CHAT,
                json={"model": "gpt-4o", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
            ) as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                lines = [ln async for ln in r.aiter_lines()]

    payloads = [ln[len("data:"):].strip() for ln in lines if ln.startswith("data:")]
    assert payloads[-1] == "[DONE]", "a harness waits for the sentinel"
    deltas = [json.loads(p) for p in payloads[:-1]]
    text = "".join(d["choices"][0]["delta"].get("content", "") for d in deltas)
    assert text == "Hallo"
    assert any(d["object"] == "chat.completion.chunk" for d in deltas)

    async with app_session(tenant) as db:
        row = (
            await db.execute(
                select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)
            )
        ).scalar_one()
        assert row.tokens_in == 11 and row.tokens_out == 2


async def test_a_stream_with_no_reported_usage_still_bills_something(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Billing zero would hide a runaway agent from its budget. The estimate is
    crude on purpose; being approximately right beats being invisibly wrong."""
    from sqlalchemy import select

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)

    async def fake_stream(*a: object, **kw: object) -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(text="x" * 400)
        yield CompletionChunk(stop_reason="stop")  # no usage, ever

    monkeypatch.setattr("oc8.api.llm_gateway.stream_completion_with_fallback", fake_stream)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            async with c.stream(
                "POST", CHAT,
                json={"model": "gpt-4o", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
            ) as r:
                _ = [ln async for ln in r.aiter_lines()]

    async with app_session(tenant) as db:
        row = (
            await db.execute(
                select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)
            )
        ).scalar_one()
        assert row.tokens_out > 0, "an unreported stream must not bill zero"


async def test_billing_survives_the_cancellation_of_its_own_request(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that hangs up mid-stream must still be charged for what it used.

    The mechanism, which took two wrong fixes to pin down: when the client
    disconnects, the whole request task is CANCELLED. In a `finally`, every await
    then raises CancelledError at once -- including opening a database session. So
    neither "bill in finally" nor "bill on a separate session" is enough; the write
    has to be detached from the cancelled task to survive at all.

    Reproduced by throwing CancelledError into the generator, which is what
    Starlette does on disconnect. Two earlier versions of this test passed against
    broken code because they closed the stream politely instead.
    """
    import asyncio

    from sqlalchemy import func, select

    from oc8.api.llm_gateway import GatewayCaller, _sse
    from oc8.db.session import tenant_session

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)

    async def fake_stream(*a: object, **kw: object) -> AsyncIterator[CompletionChunk]:
        for _ in range(50):
            yield CompletionChunk(text="wort ")
        yield CompletionChunk(stop_reason="stop", usage=Usage(tokens_in=9, tokens_out=50))

    monkeypatch.setattr("oc8.api.llm_gateway.stream_completion_with_fallback", fake_stream)

    async with tenant_session(tenant) as request_db:
        agent = await request_db.get(m.Agent, agent_id)
        assert agent is not None
        caller = GatewayCaller(tenant_id=tenant, agent=agent, run_id=run_id, pinned={})
        gen = _sse(
            request_db, caller,
            common={
                "tenant_id": tenant, "agent_id": agent_id, "primary": None,
                "no_config_provider": "openai_compatible", "no_config_model": "m",
                "messages": [], "tools": [], "params": None,
                "request_id": uuid.uuid4(), "contains_restricted": False,
            },
            request_id=uuid.uuid4(), provider="openai_compatible", model="m",
        )
        # Drive the stream in its own task and CANCEL that task -- which is what
        # Starlette does when the socket goes away. Cancelling the task (rather
        # than closing the generator politely) is the whole point: it makes every
        # await inside `finally` raise CancelledError immediately.
        started = asyncio.Event()

        async def drive() -> None:
            async for _ in gen:
                started.set()

        task = asyncio.create_task(drive())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await request_db.rollback()

    for _ in range(80):
        async with app_session(tenant) as db:
            n = (
                await db.execute(
                    select(func.count()).select_from(m.TokenUsageRecord).where(
                        m.TokenUsageRecord.tenant_id == tenant
                    )
                )
            ).scalar_one()
        if n:
            break
        await asyncio.sleep(0.05)
    assert n == 1, "a cancelled stream must not be free"


# ------------------------------------------------------- upstream failures


def _observable(caplog: pytest.LogCaptureFixture) -> None:
    """Alembic's fileConfig (run once per session by the `settings_env` fixture,
    with disable_existing_loggers defaulting to True) disables every logger that
    already exists -- including the gateway's, created at import time. Undo that,
    or caplog observes nothing (same root cause as tests/skills/test_runtime.py)."""
    logging.getLogger("oc8.api.llm_gateway").disabled = False


async def test_an_upstream_rejection_reports_the_providers_own_reason(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 502 that says only "upstream failed" makes the operator reproduce the
    call by hand. The provider's message must reach both the client and the log."""
    import httpx as _httpx

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)

    reason = "Unexpected role 'system' after role 'tool'"

    async def fake_complete(*a: object, **kw: object) -> CompletionResult:
        request = _httpx.Request("POST", "https://api.opaas.ai/v1/chat/completions")
        response = _httpx.Response(400, json={"error": {"message": reason}}, request=request)
        raise _httpx.HTTPStatusError(
            f"400 from upstream: {reason}", request=request, response=response
        )

    monkeypatch.setattr("oc8.api.llm_gateway.complete_with_fallback", fake_complete)

    _observable(caplog)
    with caplog.at_level(logging.WARNING, logger="oc8.api.llm_gateway"):
        code, body, _h = await _post(
            _agent_token(tenant, agent_id, run_id),
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert code == 502
    assert reason in body["detail"]
    assert any(reason in r.getMessage() for r in caplog.records), "and readable in the log"


async def test_a_stream_that_dies_says_why_in_the_log_too(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once the 200 is sent the only channel left is an in-band error frame, which
    a harness often swallows. Without a log line the failure leaves no trace."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant)

    reason = "context length exceeded: 200000 > 32768"

    async def fake_stream(*a: object, **kw: object) -> AsyncIterator[CompletionChunk]:
        raise RuntimeError(reason)
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr("oc8.api.llm_gateway.stream_completion_with_fallback", fake_stream)

    app = create_app()
    _observable(caplog)
    with caplog.at_level(logging.WARNING, logger="oc8.api.llm_gateway"):
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                async with c.stream(
                    "POST", CHAT,
                    json={"model": "gpt-4o", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
                ) as r:
                    lines = [ln async for ln in r.aiter_lines()]

    payloads = [ln[len("data:"):].strip() for ln in lines if ln.startswith("data:")]
    frames = [json.loads(p) for p in payloads if p != "[DONE]"]
    assert any(reason in json.dumps(f.get("error", {})) for f in frames), "in-band error frame"
    assert any(reason in r.getMessage() for r in caplog.records), "and a server-side log line"
