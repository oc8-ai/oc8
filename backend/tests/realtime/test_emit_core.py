from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import redis.asyncio as redis

from oc8 import models as m
from oc8.realtime.bus import channel_for
from oc8.realtime.emit import publish_agent_status, record_activity
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _drain(pubsub) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg is not None:
            data: dict[str, Any] = json.loads(msg["data"])
            return data
    return None


async def test_transition_publishes_run_status(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant_id = uuid.uuid4()
    sub = redis.from_url(redis_url, decode_responses=True)
    pubsub = sub.pubsub()
    await pubsub.subscribe(channel_for(tenant_id))
    await pubsub.get_message(timeout=1.0)
    try:
        async with app_session(tenant_id) as db:
            repo = RunRepository(db)
            run = await repo.create(
                tenant_id=tenant_id, agent_id=uuid.uuid4(), context={"task": "t"}
            )
            await repo.transition(run, RunState.RUNNING)
            env = await _drain(pubsub)
        assert env is not None
        assert env["type"] == "run.status"
        assert env["data"]["state"] == "running"
        assert env["data"]["run_id"] == str(run.id)
        assert env["data"]["source"] == run.source
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await sub.aclose()


async def test_publish_agent_status(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant_id = uuid.uuid4()
    sub = redis.from_url(redis_url, decode_responses=True)
    pubsub = sub.pubsub()
    await pubsub.subscribe(channel_for(tenant_id))
    await pubsub.get_message(timeout=1.0)
    try:
        async with app_session(tenant_id) as db:
            agent = m.Agent(
                tenant_id=tenant_id, department_id=uuid.uuid4(), name="a", status="running"
            )
            db.add(agent)
            await db.flush()
            await publish_agent_status(agent)
            env = await _drain(pubsub)
        assert env is not None and env["type"] == "agent.status"
        assert env["data"] == {"agent_id": str(agent.id), "status": "running"}
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await sub.aclose()


async def test_record_activity_writes_and_publishes(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant_id = uuid.uuid4()
    sub = redis.from_url(redis_url, decode_responses=True)
    pubsub = sub.pubsub()
    await pubsub.subscribe(channel_for(tenant_id))
    await pubsub.get_message(timeout=1.0)
    try:
        async with app_session(tenant_id) as db:
            row = await record_activity(
                db, tenant_id=tenant_id, agent_id=None, status="info", message="hello"
            )
            env = await _drain(pubsub)
        assert row.message == "hello"
        assert env is not None and env["type"] == "activity.logged"
        assert env["data"]["message"] == "hello"
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await sub.aclose()
