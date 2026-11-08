"""Recording an operator's request to stop a run (§7.2).

Shared by the admin `POST /runs/{id}/cancel` (`run:control`) and the chat's
`POST /runs/{id}/stop` (the run's own authority, `oc8.runtime.run_inputs`):
two doors, one cancel. Each door decides WHO may stop; this decides nothing
about that and only writes the signal.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import append_event

if TYPE_CHECKING:
    from oc8.auth.principal import Principal


async def record_operator_cancel(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    principal: Principal,
    actor_id: uuid.UUID | None,
    resource_extra: dict[str, Any] | None = None,
) -> None:
    """Record the cancel on an UNLOCKED row. NEVER write agent_run here: a
    running executor holds that row's lock for the whole run, so a write would
    block until the run ends. The executor is the sole agent_run.state mutator
    and honors this row -- skipping a queued run before start, or stopping a
    running run at its next step boundary. Idempotent: a second cancel adds no
    row. The caller has already refused a terminal run and commits."""
    existing = (
        await db.execute(select(m.RunCancellation).where(m.RunCancellation.run_id == run.id))
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            m.RunCancellation(
                tenant_id=run.tenant_id,
                run_id=run.id,
                requested_at=dt.datetime.now(tz=dt.UTC),
                cancellation_kind="operator_interrupted",
            )
        )

    await append_event(
        db,
        tenant_id=run.tenant_id,
        actor_type="operator",
        actor_id=actor_id,
        category="run",
        action="run.cancel",
        resource={"run_id": str(run.id), "was_state": run.state, **(resource_extra or {})},
        decision="allow",
        principal=principal,
    )
