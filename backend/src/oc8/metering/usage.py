"""Usage recording.

Records are keyed by ``request_id`` and inserted with ON CONFLICT DO NOTHING so
at-least-once producers (the Model Router) never double-count. The table grants
no UPDATE/DELETE to the runtime role (migration 0001), so records are immutable.

Cost is deliberately NOT computed here (see the 2026-08-19 Cost Center design
spec, Part A): this function runs on the completion hot path, and pricing now
lives in a DB-backed, versioned table that a hot-path write must never query.
Cost is computed lazily, only when a report is rendered (metering/pricing.py's
price_as_of, called from api/v1/feed.py).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.connections.acting import resolve_acting_person
from oc8.models.ops import TokenUsageRecord
from oc8.observability import record_token_usage

logger = logging.getLogger(__name__)


async def record_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    model: str,
    provider: str,
    tokens_in: int,
    tokens_out: int,
    agent_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    platform_units: int = 0,
    skill_id: uuid.UUID | None = None,
    skill_version_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    cache_hit: bool = False,
    saved_tokens_in: int = 0,
    saved_tokens_out: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    member_id: uuid.UUID | None = None,
) -> None:
    stmt = (
        pg_insert(TokenUsageRecord)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            request_id=request_id,
            model=model,
            provider=provider,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            agent_id=agent_id,
            department_id=department_id,
            platform_units=platform_units,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            creator_id=creator_id,
            cache_hit=cache_hit,
            saved_tokens_in=saved_tokens_in,
            saved_tokens_out=saved_tokens_out,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            member_id=member_id,
        )
        .on_conflict_do_nothing(index_elements=["request_id"])
    )
    await session.execute(stmt)
    record_token_usage(
        provider=provider,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


async def usage_member_id(session: AsyncSession, run: m.AgentRun | None) -> uuid.UUID | None:
    """The member a run's token usage is attributed to: the person it acts for
    (`resolve_acting_person`), or None.

    Resolved by the caller once per run or request, never inside
    `record_usage` (a single INSERT on the hot path). Attribution is
    best-effort: a failed lookup leaves the usage unattributed instead of
    failing the model call or the bill, and runs in a savepoint so the
    caller's transaction stays usable."""
    if run is None:
        return None
    try:
        async with session.begin_nested():
            return (await resolve_acting_person(session, run)).member_id
    except Exception:
        logger.warning(
            "usage: could not resolve the acting person for run %s", run.id, exc_info=True
        )
        return None
