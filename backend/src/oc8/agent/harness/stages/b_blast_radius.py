"""B7 -- bound how many distinct records one run may change."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.agent.blast_radius import records_per_run, records_touched, refusal
from oc8.agent.claims import held_by_run


async def check_blast_radius(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None,
    frame: dict[str, Any] | None,
    identity: tuple[str, str] | None,
) -> str | None:
    """Return the B7 refusal, or ``None`` when this write stays in bounds."""
    if run_id is None or identity is None:
        return None

    entity, ref = identity
    if await held_by_run(
        db,
        tenant_id=tenant_id,
        entity=entity,
        record_ref=ref,
        run_id=run_id,
    ):
        return None

    limit = records_per_run(frame)
    touched = await records_touched(db, tenant_id=tenant_id, run_id=run_id)
    if limit and touched >= limit:
        return refusal(touched, limit)
    return None
