"""What an isolated run is waiting for before its first step.

A container run can sit for minutes before the agent does anything: on a
cold cluster the sandbox waits for a new node and an image pull. The run
view showed nothing meanwhile and people assumed nothing was happening. So
the isolated runtime marks the run on `agent_run.phase` (already on RunDTO
and on every `run.status` event):

- "provisioning" while the driver places and starts the sandbox,
- "starting" once the container runs and its shell has not asked for a
  step yet,
- cleared (NULL) by the first step (`/internal/agent/{id}/step`) and, as a
  backstop, when the leg ends.

Each change is its own short transaction -- the executor's session holds the
run's state transition and must not hold the row while a container runs --
and a guarded UPDATE, so a step that already cleared the phase is never
overwritten by a late "starting". Each change goes out as `run.status` with
the run's current state, which the frontend's patcher applies as it is.

Cosmetic by design: a failure here is logged, never raised into the run.
In-process runs never set it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from typing import Final

from sqlalchemy import text

from oc8.db.session import tenant_session
from oc8.realtime.bus import get_event_bus

logger = logging.getLogger(__name__)

PROVISIONING: Final = "provisioning"
STARTING: Final = "starting"
START_PHASES: Final = frozenset({PROVISIONING, STARTING})

_SET = text(
    "UPDATE agent_run SET phase = :phase "
    "WHERE id = :id AND (phase = ANY(:expect) OR (:from_none AND phase IS NULL)) "
    "RETURNING agent_id, state"
)


async def set_start_phase(
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    phase: str | None,
    *,
    expect: Collection[str | None],
) -> bool:
    """Set the run's phase to `phase` if it currently is one of `expect`
    (None meaning "no phase"); publish `run.status` when it changed."""
    try:
        async with tenant_session(tenant_id) as db:
            row = (
                await db.execute(
                    _SET,
                    {
                        "phase": phase,
                        "id": run_id,
                        "expect": [p for p in expect if p is not None],
                        "from_none": None in expect,
                    },
                )
            ).one_or_none()
    except Exception:
        logger.warning("run %s: could not set its start phase to %s", run_id, phase, exc_info=True)
        return False
    if row is None:
        return False
    await get_event_bus().publish_event(
        tenant_id,
        "run.status",
        {"run_id": str(run_id), "agent_id": str(row.agent_id), "state": row.state, "phase": phase},
        source=f"oc8/run/{run_id}",
    )
    return True


async def clear_start_phase(tenant_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """The run's first step (or its leg's end): nothing to wait for any more."""
    return await set_start_phase(tenant_id, run_id, None, expect=START_PHASES)
