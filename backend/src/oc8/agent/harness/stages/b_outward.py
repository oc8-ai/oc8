"""B8 -- one outward message per recipient per task (spec §5 B8).

The check runs BEFORE the call, not after: the point is that the recipient is
not reached twice, and a check that ran afterwards could only report it. The
logic lives in `oc8.agent.outward`; this module is the call site both
runtimes used to carry inline, moved here unchanged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.agent.outward import REFUSAL, already_delivered, outward_target, remember_delivery
from oc8.modelrouter import ToolCall


@dataclass(frozen=True)
class OutwardCheck:
    #: The recipient this call reaches; None when the tool reaches nobody.
    target: str | None
    #: The refusal to hand the model instead of calling, when the recipient was
    #: already reached in this task. None -> go ahead.
    refusal: str | None


async def check_outward(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID | None,
    tc: ToolCall,
    focus_spec: dict[str, Any] | None,
    outward_tools: list[str] | None,
) -> OutwardCheck:
    target = outward_target(tc.name, tc.arguments, focus_spec, outward_tools)
    if target is None or task_id is None:
        return OutwardCheck(target=target, refusal=None)
    if await already_delivered(db, tenant_id=tenant_id, task_id=task_id, target=target):
        return OutwardCheck(target=target, refusal=REFUSAL.format(target=target))
    return OutwardCheck(target=target, refusal=None)


async def remember_outward(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID | None,
    target: str | None,
    output: str,
) -> None:
    """Record a delivery -- only a successful one; a failed send did not reach
    anybody and must stay retryable."""
    if target is None or task_id is None or output.startswith("ERROR:"):
        return
    await remember_delivery(db, tenant_id=tenant_id, task_id=task_id, target=target)
