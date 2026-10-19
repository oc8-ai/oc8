"""Whether an organisation may run background work.

A single-organisation deployment registers nothing here: every organisation
may run. An embedding deployment that can take an organisation out of
service registers a gate at startup; scheduled runs, queued and running
runs, channel polling and ingestion then honour it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

type TenantActivityGate = Callable[[uuid.UUID], Awaitable[bool]]

_gate: TenantActivityGate | None = None


class TenantInactive(Exception):
    """The organisation may not run background work right now."""

    def __init__(self, tenant_id: uuid.UUID) -> None:
        super().__init__(f"organisation {tenant_id} is not active")
        self.tenant_id = tenant_id


def set_tenant_activity_gate(gate: TenantActivityGate | None) -> None:
    global _gate
    _gate = gate


def get_tenant_activity_gate() -> TenantActivityGate | None:
    return _gate


async def tenant_may_run(tenant_id: uuid.UUID) -> bool:
    gate = _gate
    if gate is None:
        return True
    try:
        return bool(await gate(tenant_id))
    except Exception:
        # Keep the deployment's background work alive; a deployment that
        # wants to stop work on errors answers False itself.
        logger.exception("activity gate failed for organisation %s", tenant_id)
        return True
