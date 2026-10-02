"""Task A3: a run created through `enqueue_run` (HTTP, chat, cron, webhook)
is pinned to the agent's current version, so it executes against one
immutable configuration from intake to finish.

The pin itself lives in `RunRepository.create`, which `enqueue_run` calls.
The run-creation paths that bypass `enqueue_run` (a delegated sub-run, a lead's
wake-up) are covered in `test_every_run_is_pinned.py`."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.runtime.intake import enqueue_run
from oc8.runtime.queue import RunQueue
from tests.conftest import AppSessionFactory
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio


def _isolated_queue(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> RunQueue:
    queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    return queue


async def test_enqueue_run_pins_the_current_agent_version(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    agent = await _make_agent(app_session)  # publishes v1
    assert agent.current_version_id is not None
    try:
        async with app_session(agent.tenant_id) as db:
            run, published = await enqueue_run(
                db,
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                context={"task": "anything"},
                source="manual",
            )
    finally:
        await queue.close()
    assert published is True
    assert run.agent_version_id == agent.current_version_id

    async with app_session(agent.tenant_id) as db:
        row = await db.get(m.AgentRun, run.id)
        assert row is not None
        assert row.agent_version_id == agent.current_version_id


async def test_a_later_publish_does_not_move_an_already_queued_run(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    agent = await _make_agent(app_session, mission="first")
    v1 = agent.current_version_id
    try:
        async with app_session(agent.tenant_id) as db:
            run, _ = await enqueue_run(
                db,
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                context={},
                source="manual",
            )
    finally:
        await queue.close()

    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        a.mission = "second"
        v2 = await publish_version(db, a)
    assert v2.id != v1

    async with app_session(agent.tenant_id) as db:
        row = await db.get(m.AgentRun, run.id)
        assert row is not None
        assert row.agent_version_id == v1


async def test_enqueue_run_for_an_agent_without_a_row_leaves_the_pin_empty(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intake must not start failing for an agent id it cannot resolve (the
    existing intake tests enqueue against bare uuids); `resolve_version` falls
    back to a live snapshot for an unpinned run."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            run, _ = await enqueue_run(
                db, tenant_id=tenant, agent_id=uuid.uuid4(), context={}, source="manual"
            )
    finally:
        await queue.close()
    assert run.agent_version_id is None
