"""Whether an organisation may run background work.

A single-organisation deployment registers nothing here: every organisation
may run. An embedding deployment that can take an organisation out of
service registers a gate at startup with `set_tenant_activity_gate`. These
then honour it:
- scheduled (cron) runs and follow-ups,
- event and webhook triggers,
- queued and running runs,
- channel polling,
- ingestion.

Fan-out code that should only visit organisations that may run lists them
with `oc8.triggers.scheduler.list_runnable_tenant_ids`.

Asked once per step of every running run; a gate must be cheap or cached.
A gate that raises counts as "may run"; its error is logged at most once a
minute per organisation.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

type TenantActivityGate = Callable[[uuid.UUID], Awaitable[bool]]

_gate: TenantActivityGate | None = None

#: The cancellation_kind of a run the gate stopped, and a follow-up's skip reason.
TENANT_INACTIVE_KIND = "tenant_inactive"
#: Why a piece of background work (e.g. an ingestion job) was dropped.
TENANT_INACTIVE_REASON = "organisation is not active"
#: What a run stopped by the gate reports, as its output and in the activity feed.
TENANT_INACTIVE_OUTPUT = "Cancelled: organisation is not active"

#: A raising gate is logged in full at most once per organisation in this window.
_ERROR_LOG_INTERVAL_S = 60.0
_last_error_logged: dict[uuid.UUID, float] = {}


class TenantInactive(Exception):
    """The organisation may not run background work right now."""

    def __init__(self, tenant_id: uuid.UUID) -> None:
        super().__init__(f"organisation {tenant_id} is not active")
        self.tenant_id = tenant_id


def set_tenant_activity_gate(gate: TenantActivityGate | None) -> None:
    """Register the deployment's gate, or None to let every organisation run."""
    global _gate
    _gate = gate
    _last_error_logged.clear()


def get_tenant_activity_gate() -> TenantActivityGate | None:
    """The registered gate, or None when every organisation may run."""
    return _gate


async def tenant_may_run(tenant_id: uuid.UUID) -> bool:
    gate = _gate
    if gate is None:
        return True
    try:
        return bool(await gate(tenant_id))
    except Exception:
        # Keep the deployment's background work alive; a deployment that
        # wants to stop work on errors answers False itself. Asked at every
        # step of every run, so a broken gate must not flood the log.
        now = time.monotonic()
        last = _last_error_logged.get(tenant_id)
        if last is None or now - last >= _ERROR_LOG_INTERVAL_S:
            _last_error_logged[tenant_id] = now
            logger.exception("activity gate failed for organisation %s", tenant_id)
        else:
            logger.debug("activity gate failed for organisation %s", tenant_id, exc_info=True)
        return True
