"""The approval park path tells a messenger sender "waiting for approval"
only when no always-allow rule answered the held card."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8.agent.engine import RunResult
from oc8.runtime.executor import _CHAT_WAITING_FOR_APPROVAL
from tests.conftest import AppSessionFactory
from tests.runtime.test_carried_over_inputs import _execute, _world
from tests.runtime.test_executor import _FnRuntime, _RecordingChannels

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(("rule_answered", "told"), [(True, 0), (False, 1)])
async def test_a_rule_approved_call_is_not_reported_as_waiting(
    app_session: AppSessionFactory,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    rule_answered: bool,
    told: int,
) -> None:
    world = await _world(app_session, messenger=True)
    fake = _RecordingChannels(app_session, world.tenant, world.run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def _rules(_db: Any, run: Any) -> list[uuid.UUID]:
        return [run.id] if rule_answered else []

    monkeypatch.setattr("oc8.runtime.executor._auto_decide_by_rules", _rules)

    async def fn(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="waiting_for_approval",
            output="",
            tool_calls=[],
            steps=1,
        )

    await _execute(world, _FnRuntime(fn))

    assert [text for _ext, text in fake.said].count(_CHAT_WAITING_FOR_APPROVAL) == told
