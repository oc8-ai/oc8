"""B9 -- replay-safety for writes (spec §5 B9).

A restarted task replaying the same write gets the first result rather than
acting twice. Reads are exempt on purpose: deduplicating a search would hide
the very changes the agent is meant to observe. Only a successful side effect
is recorded; recording a failure would answer a legitimate retry with the old
error forever. Wired into the isolated runtime only in package 1 (spec §1.1).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.agent.tool_idempotency import record_invocation, replayed_result
from oc8.modelrouter import ToolCall


async def replay_for(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID | None,
    tc: ToolCall,
    writes: bool,
) -> str | None:
    if not writes or task_id is None:
        return None
    return await replayed_result(
        db, tenant_id=tenant_id, task_id=task_id, tool=tc.name, arguments=tc.arguments
    )


async def record_for(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID | None,
    tc: ToolCall,
    writes: bool,
    output: str,
) -> None:
    if not writes or task_id is None or output.startswith("ERROR:"):
        return
    await record_invocation(
        db,
        tenant_id=tenant_id,
        task_id=task_id,
        tool=tc.name,
        arguments=tc.arguments,
        result=output,
    )
