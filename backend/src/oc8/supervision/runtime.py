"""Adapters that compose supervision into the run-loop seam."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.models.supervision import TaskAnchor
from oc8.runtime.supervision_hook import SupervisionQueryPort, SupervisionRunHook
from oc8.supervision.loop_hook import maybe_checkpoint, maybe_create_anchor


class SupervisionQueryPortImpl:
    """Query port backed by the supervision assignment table."""

    async def has_supervision(self, db: AsyncSession, *, agent_id: uuid.UUID) -> bool:
        from oc8.supervision.service import get_supervisor

        return await get_supervisor(db, supervised_agent_id=agent_id) is not None


SUPERVISION_QUERY_PORT: SupervisionQueryPort = SupervisionQueryPortImpl()


class SupervisionRunHookImpl:
    """Expose the loop hook through the run-loop protocol."""

    async def create_anchor(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        task_id: uuid.UUID,
        task_text: str,
    ) -> object | None:
        return await maybe_create_anchor(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            task_id=task_id,
            task_text=task_text,
        )

    async def checkpoint(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        task_id: uuid.UUID,
        anchor: object | None,
        tool_trace_delta: list[dict[str, Any]],
        tokens_since_checkpoint: int,
        force: bool,
        contains_restricted: bool = False,
    ) -> object | None:
        typed_anchor = anchor if isinstance(anchor, TaskAnchor) else None
        return await maybe_checkpoint(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            task_id=task_id,
            anchor=typed_anchor,
            tool_trace_delta=tool_trace_delta,
            tokens_since_checkpoint=tokens_since_checkpoint,
            force=force,
            contains_restricted=contains_restricted,
        )


SUPERVISION_RUN_HOOK: SupervisionRunHook = SupervisionRunHookImpl()
