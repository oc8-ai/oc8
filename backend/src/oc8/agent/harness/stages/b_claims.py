"""B6 -- claim a named record before changing it."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.agent.claims import claim_record, refusal


async def claim_write(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    identity: tuple[str, str] | None,
    label: str,
) -> str | None:
    """Claim this write's record, returning the holder refusal on conflict."""
    if run_id is None or identity is None:
        return None

    entity, ref = identity
    holder = await claim_record(
        db,
        tenant_id=tenant_id,
        entity=entity,
        record_ref=ref,
        run_id=run_id,
        agent_id=agent_id,
    )
    if holder is None:
        return None
    return refusal(label or f"{entity} {ref}", holder)
