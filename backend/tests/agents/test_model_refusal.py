"""A model may decline a request: HTTP 200, `stop_reason == "refusal"`, and no
content (or only the part written before a classifier stepped in). Read as an
ordinary empty answer that was a run reported "done" with nothing done.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import CompletionResult, Usage, chunk_from_result
from oc8.modelrouter.sampling import refusal_message
from oc8.notifications.events import failure_category
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _Declines:
    def __init__(self, text: str = "") -> None:
        self.calls = 0
        self._text = text

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        return CompletionResult(
            text=self._text,
            tool_calls=[],
            usage=Usage(900, 0),
            stop_reason="refusal",
            provider="anthropic",
            model="claude-opus-5-5",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _run(app_session: AppSessionFactory, router: _Declines) -> Any:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="IT-Support", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Lennart")
        db.add(agent)
        await db.flush()
        return await run_agent(db, agent=agent, task_text="Handle the ticket", tenant_id=tenant)


async def test_a_declined_request_fails_the_run_instead_of_finishing_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _Declines()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    result = await _run(app_session, router)
    assert result.status == "failed"
    assert result.output == refusal_message()
    # Asking again would only be declined again.
    assert router.calls == 1


async def test_a_refusal_never_passes_on_what_the_model_wrote_before_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _Declines(text="Here is the first half of")
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    result = await _run(app_session, router)
    assert result.status == "failed"
    assert "first half" not in result.output


def test_a_refusal_counts_as_a_model_failure_for_notifications() -> None:
    run = m.AgentRun(context={"output": refusal_message()})
    assert failure_category(run) == "model_error"
