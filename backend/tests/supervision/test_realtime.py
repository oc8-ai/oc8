"""Supervision interventions emit events through the realtime transport."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import redis.asyncio as redis

from oc8.realtime.bus import channel_for
from oc8.supervision.intervention import record_intervention
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _drain(pubsub: Any) -> dict[str, Any] | None:
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message is not None:
            value: dict[str, Any] = json.loads(message["data"])
            return value
    return None


async def test_record_intervention_publishes(
    app_session: AppSessionFactory,
    redis_url: str,
) -> None:
    tenant_id = uuid.uuid4()
    subscriber = redis.from_url(redis_url, decode_responses=True)
    pubsub = subscriber.pubsub()
    await pubsub.subscribe(channel_for(tenant_id))
    await pubsub.get_message(timeout=1.0)
    try:
        supervised = uuid.uuid4()
        async with app_session(tenant_id) as db:
            intervention = await record_intervention(
                db,
                tenant_id=tenant_id,
                supervisor_agent_id=uuid.uuid4(),
                supervised_agent_id=supervised,
                task_id=uuid.uuid4(),
                kind="steer",
                reason={"score": 0.4},
            )
            envelope = await _drain(pubsub)

        assert envelope is not None and envelope["type"] == "supervision.intervention"
        assert envelope["data"]["intervention_id"] == str(intervention.id)
        assert envelope["data"]["supervised_agent_id"] == str(supervised)
        assert envelope["data"]["kind"] == "steer"
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await subscriber.aclose()
