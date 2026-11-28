"""The isolated runtime's /step stops a run for the same reasons as the
engine (oc8.agent.run_limits), with no model call: its token limit, and a
call repeated past every reminder."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8.agent.run_limits import NO_PROGRESS_OUTPUT, REPEAT_LIMIT, TOKEN_LIMIT_OUTPUT
from oc8.config import get_settings
from tests.api.test_internal_agent import _plain_agent_run, _post_step
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*args: object, **kw: object) -> Any:
        raise AssertionError("no model call once the run is over its limit")
        yield  # pragma: no cover

    monkeypatch.setattr("oc8.api.v1.internal_agent.stream_completion_with_fallback", refuse)


async def test_step_stops_a_run_that_used_its_token_limit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_model(monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_max_run_tokens", 40_000)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(
            db,
            tenant,
            steps=2,
            stepTimings=[
                {"step": 1, "tokens_in": 21_000, "tokens_out": 300},
                {"step": 2, "tokens_in": 21_000, "tokens_out": 300},
            ],
        )
        agent_id, run_id = agent.id, run.id
        await db.commit()

    body = await _post_step(tenant, agent_id, run_id)
    assert body["done"] is True
    assert body["text"] == TOKEN_LIMIT_OUTPUT


async def test_step_stops_a_run_repeating_the_same_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_model(monkeypatch)
    call = {"tool": "odoo_search", "arguments": {"model": "calendar.event"}, "state": "done"}
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(
            db, tenant, steps=REPEAT_LIMIT, toolCalls=[call] * REPEAT_LIMIT
        )
        agent_id, run_id = agent.id, run.id
        await db.commit()

    body = await _post_step(tenant, agent_id, run_id)
    assert body["done"] is True
    assert body["text"] == NO_PROGRESS_OUTPUT
