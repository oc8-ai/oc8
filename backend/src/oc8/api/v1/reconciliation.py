"""On-demand Anthropic/OpenAI cost reconciliation (Cost Center design,
2026-08-19 spec Part C). No scheduled job -- a human clicks "Refresh" on the
Cost Center page. A provider with no stored admin key is silently skipped
(opt-in feature, not an error)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import MANAGE, MODEL, perm
from oc8.metering.pricing import active_price_rows, cost_micros_from_price, price_as_of
from oc8.metering.reconciliation import fetch_anthropic_cost, fetch_openai_cost
from oc8.modelrouter.keys import model_admin_key_ref
from oc8.schemas.dto import ReconciliationDTO
from oc8.secrets.service import SecretNotFound, resolve_secret

logger = logging.getLogger(__name__)

router = APIRouter()

_FETCHERS = {"anthropic": fetch_anthropic_cost, "openai": fetch_openai_cost}
_LOOKBACK_DAYS = 30


async def _oc8_calculated_cost_for_day(
    db: DbSession, *, tenant_id: uuid.UUID, provider: str, day: dt.date
) -> int:
    rows = (
        await db.execute(
            select(
                m.TokenUsageRecord.model,
                m.TokenUsageRecord.tokens_in,
                m.TokenUsageRecord.tokens_out,
                m.TokenUsageRecord.cache_read_tokens,
                m.TokenUsageRecord.cache_write_tokens,
            ).where(
                m.TokenUsageRecord.tenant_id == tenant_id,
                m.TokenUsageRecord.provider == provider,
                m.TokenUsageRecord.ts >= dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC),
                m.TokenUsageRecord.ts
                < dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC),
            )
        )
    ).all()
    price_rows = await active_price_rows(
        db, as_of=dt.datetime.combine(day, dt.time.max, tzinfo=dt.UTC)
    )
    total = 0
    for model, tokens_in, tokens_out, cache_read, cache_write in rows:
        price = price_as_of(
            price_rows, provider, model, dt.datetime.combine(day, dt.time.max, tzinfo=dt.UTC)
        )
        if price:
            total += cost_micros_from_price(
                price,
                tokens_in,
                tokens_out,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
    return total


@router.post(
    "/model-cost-reconciliation/refresh",
    response_model=list[ReconciliationDTO],
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def refresh_reconciliation(
    db: DbSession, principal: CurrentPrincipal
) -> list[ReconciliationDTO]:
    until = dt.date.today()
    since = until - dt.timedelta(days=_LOOKBACK_DAYS)
    out: list[ReconciliationDTO] = []
    for provider, fetcher in _FETCHERS.items():
        try:
            admin_key = await resolve_secret(
                db, tenant_id=principal.tenant_id, ref=model_admin_key_ref(provider)
            )
        except SecretNotFound:
            continue  # opt-in -- no key means this provider is simply skipped
        try:
            reported = await fetcher(admin_key, since=since, until=until)
        except (httpx.HTTPStatusError, httpx.RequestError):
            # A present-but-broken admin key (expired/revoked/rejected) or a
            # connection-level failure must not fail the whole request -- the
            # other provider's (working) result is still worth returning and
            # persisting. Logged so the failure stays visible, not silent.
            logger.warning(
                "model-cost-reconciliation fetch failed for provider=%s", provider, exc_info=True
            )
            continue
        for day, reported_cost in reported.items():
            calculated = await _oc8_calculated_cost_for_day(
                db, tenant_id=principal.tenant_id, provider=provider, day=day
            )
            stmt = (
                pg_insert(m.ModelCostReconciliation)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=principal.tenant_id,
                    provider=provider,
                    report_date=day,
                    oc8_calculated_cost_micros=calculated,
                    provider_reported_cost_micros=reported_cost,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "provider", "report_date"],
                    set_={
                        "oc8_calculated_cost_micros": calculated,
                        "provider_reported_cost_micros": reported_cost,
                        "fetched_at": dt.datetime.now(dt.UTC),
                    },
                )
            )
            await db.execute(stmt)
            out.append(
                ReconciliationDTO(
                    provider=provider,
                    report_date=day.isoformat(),
                    oc8_calculated_cost_micros=calculated,
                    provider_reported_cost_micros=reported_cost,
                    fetched_at=dt.datetime.now(dt.UTC).isoformat(),
                )
            )
    await db.commit()
    return out


@router.get(
    "/model-cost-reconciliation",
    response_model=list[ReconciliationDTO],
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def list_reconciliation(
    db: DbSession, principal: CurrentPrincipal
) -> list[ReconciliationDTO]:
    rows = (
        (
            await db.execute(
                select(m.ModelCostReconciliation)
                .where(m.ModelCostReconciliation.tenant_id == principal.tenant_id)
                .order_by(m.ModelCostReconciliation.report_date.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        ReconciliationDTO(
            provider=r.provider,
            report_date=r.report_date.isoformat(),
            oc8_calculated_cost_micros=r.oc8_calculated_cost_micros,
            provider_reported_cost_micros=r.provider_reported_cost_micros,
            fetched_at=str(r.fetched_at),
        )
        for r in rows
    ]
