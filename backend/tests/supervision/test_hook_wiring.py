"""Checkpoint recording dispatches through the public hook bus."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8.constants import ACME_TENANT_ID
from oc8.hooks.points import register_core_points
from oc8.hooks.registry import HookRegistry
from oc8.hooks.types import HookCtx, HookHandler
from oc8.supervision.checkpoints import record_checkpoint
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_checkpoint_recorded_hook_is_observed(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    registry = HookRegistry()
    register_core_points(registry)
    hits: list[tuple[str, str]] = []

    def _record(ctx: HookCtx, **kwargs: Any) -> None:
        hits.append((kwargs["checkpoint_id"], kwargs["agent_id"]))

    registry.register_handler(
        HookHandler("p1", "supervision.checkpoint.recorded", 50, False, _record, None)
    )
    monkeypatch.setattr("oc8.hooks.bus.get_hook_registry", lambda tenant_id: registry)

    async with app_session(tenant) as db:
        agent_id = uuid.uuid4()
        checkpoint = await record_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            task_id=uuid.uuid4(),
            state_summary="progress",
        )

    assert hits == [(str(checkpoint.id), str(agent_id))]
