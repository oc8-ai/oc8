"""Recording an operator's request to stop a run (§7.2), and asking at a step
boundary whether the run must stop.

Shared by the admin `POST /runs/{id}/cancel` (`run:control`) and the chat's
`POST /runs/{id}/stop` (the run's own authority, `oc8.runtime.run_inputs`):
two doors, one cancel. Each door decides WHO may stop; this decides nothing
about that and only writes the signal.

`run_stop` is the other side: the probe a container run's `/step` and the MCP
gateway's `tools/call` both ask before letting the run act again, so the order
of the checks and the kinds they record cannot drift between the two doors.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import append_event
from oc8.runtime.run_inputs import (
    OWNERSHIP_CHANGED_KIND,
    OWNERSHIP_CHANGED_OUTPUT,
    steering_went_stale,
)
from oc8.tenants.activity import TENANT_INACTIVE_KIND, TENANT_INACTIVE_OUTPUT, tenant_may_run

if TYPE_CHECKING:
    from oc8.auth.principal import Principal


async def record_operator_cancel(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    principal: Principal | None,
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


#: What an operator's cancel tells the run (and the model, through the gateway).
OPERATOR_CANCEL_OUTPUT: Final = "Run cancelled by operator."


@dataclass(frozen=True)
class RunStop:
    """Why a run must not take another step."""

    #: The cancellation kind the run ends with.
    kind: str
    #: The sentence the run (and the model) is given.
    reason: str
    #: The kind the executor can only learn from the run context: the activity
    #: gate and an ownership change leave no run_cancellation row, so the
    #: caller writes this as `cancellation_kind` on the run context. None for
    #: an operator's cancel, whose row already says so.
    recorded_kind: str | None

    @property
    def context_patch(self) -> dict[str, Any]:
        """What the caller merges into the run context; empty for an operator."""
        return {"cancellation_kind": self.recorded_kind} if self.recorded_kind else {}


async def run_stop(db: AsyncSession, run: m.AgentRun) -> RunStop | None:
    """The per-step cancel probe (§7.2) for a run driven from outside the
    in-process engine, or None when it may go on. In this order, the same as
    the executor's `_cancel_check`:

    1. an operator's run_cancellation row -- an operator cancel always wins;
    2. the tenant activity gate (`tenant_inactive`);
    3. the run came to act through someone else's account after it received
       a person's words (`steering_went_stale`, `ownership_changed`).

    Reads only: the caller records `RunStop.context_patch` and commits."""
    operator_kind = (
        await db.execute(
            select(m.RunCancellation.cancellation_kind).where(m.RunCancellation.run_id == run.id)
        )
    ).scalar_one_or_none()
    if operator_kind is not None:
        return RunStop(kind=operator_kind, reason=OPERATOR_CANCEL_OUTPUT, recorded_kind=None)
    if not await tenant_may_run(run.tenant_id):
        return RunStop(
            kind=TENANT_INACTIVE_KIND,
            reason=TENANT_INACTIVE_OUTPUT,
            recorded_kind=TENANT_INACTIVE_KIND,
        )
    if await steering_went_stale(db, run):
        return RunStop(
            kind=OWNERSHIP_CHANGED_KIND,
            reason=OWNERSHIP_CHANGED_OUTPUT,
            recorded_kind=OWNERSHIP_CHANGED_KIND,
        )
    return None
