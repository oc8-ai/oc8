"""Monthly token limit per model provider (oc8 AI design 2026-10-10 §1.3).

Same window as the tenant budget: the calendar month in UTC. Usage comes from
token_usage_record, which already stores the canonical provider per call."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.metering.budget import _month_start, _next_month_start
from oc8.models.ops import ProviderBudget, TokenUsageRecord


def _canonical(provider: str) -> str:
    from oc8.modelrouter.registry import canonical_provider

    return canonical_provider(provider) or provider


@dataclass(frozen=True)
class ProviderBudgetCheck:
    provider: str
    limit: int | None
    used: int
    exhausted: bool
    resets_at: dt.datetime


async def get_provider_budget(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider: str
) -> ProviderBudget | None:
    return (
        await db.execute(
            select(ProviderBudget).where(
                ProviderBudget.tenant_id == tenant_id,
                ProviderBudget.provider == _canonical(provider),
            )
        )
    ).scalar_one_or_none()


async def set_provider_budget(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider: str, monthly_token_limit: int | None
) -> ProviderBudget:
    row = await get_provider_budget(db, tenant_id=tenant_id, provider=provider)
    if row is None:
        row = ProviderBudget(tenant_id=tenant_id, provider=_canonical(provider))
        db.add(row)
    row.monthly_token_limit = monthly_token_limit
    await db.flush()
    return row


async def provider_month_tokens(db: AsyncSession, *, tenant_id: uuid.UUID, provider: str) -> int:
    total = await db.scalar(
        select(
            func.coalesce(func.sum(TokenUsageRecord.tokens_in + TokenUsageRecord.tokens_out), 0)
        ).where(
            TokenUsageRecord.tenant_id == tenant_id,
            TokenUsageRecord.provider == _canonical(provider),
            TokenUsageRecord.ts >= _month_start(),
        )
    )
    return int(total or 0)


async def provider_tokens_this_month(db: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, int]:
    rows = await db.execute(
        select(
            TokenUsageRecord.provider,
            func.sum(TokenUsageRecord.tokens_in + TokenUsageRecord.tokens_out),
        )
        .where(TokenUsageRecord.tenant_id == tenant_id, TokenUsageRecord.ts >= _month_start())
        .group_by(TokenUsageRecord.provider)
    )
    return {provider: int(total or 0) for provider, total in rows.all()}


async def check_provider_budget(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider: str
) -> ProviderBudgetCheck:
    canonical = _canonical(provider)
    row = await get_provider_budget(db, tenant_id=tenant_id, provider=canonical)
    limit = row.monthly_token_limit if row is not None else None
    used = 0 if limit is None else await provider_month_tokens(db, tenant_id=tenant_id, provider=canonical)
    return ProviderBudgetCheck(
        provider=canonical,
        limit=limit,
        used=used,
        exhausted=limit is not None and used >= limit,
        resets_at=_next_month_start(),
    )
