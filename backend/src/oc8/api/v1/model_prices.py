"""Global, versioned model-price CRUD (Cost Center design, 2026-08-19 spec
Part A). GLOBAL, not tenant-scoped: a write here is visible to every tenant,
since provider token prices are the same for everyone. Gated the same as
/models (perm(MODEL, VIEW/MANAGE)) -- whoever can manage models can manage
prices. There is no UPDATE and no real DELETE: every write is a new version
row (see ModelPrice's docstring)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import DbSession, require_permission
from oc8.authz.permissions import MANAGE, MODEL, VIEW, perm
from oc8.config import get_settings
from oc8.schemas.dto import ModelPriceDTO
from oc8.schemas.requests import ModelPriceWrite

router = APIRouter()


def _require_writable() -> None:
    if get_settings().model_prices_read_only:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "model prices are managed by the platform operator"
        )


def _to_dto(row: m.ModelPrice) -> ModelPriceDTO:
    return ModelPriceDTO(
        id=str(row.id),
        provider=row.provider,
        model_pattern=row.model_pattern,
        price_in_usd_per_1m=float(row.price_in_usd_per_1m),
        price_out_usd_per_1m=float(row.price_out_usd_per_1m),
        effective_from=str(row.effective_from),
        active=row.active,
    )


@router.get(
    "/model-prices",
    response_model=list[ModelPriceDTO],
    dependencies=[Depends(require_permission(perm(MODEL, VIEW)))],
)
async def list_current_prices(db: DbSession) -> list[ModelPriceDTO]:
    rows = (
        (await db.execute(select(m.ModelPrice).order_by(m.ModelPrice.effective_from.desc())))
        .scalars()
        .all()
    )
    latest: dict[tuple[str, str], m.ModelPrice] = {}
    for row in rows:
        key = (row.provider, row.model_pattern)
        if key not in latest:  # rows are ordered newest-first, so first-seen = current
            latest[key] = row
    return [_to_dto(r) for r in latest.values() if r.active]


@router.get(
    "/model-prices/policy",
    dependencies=[Depends(require_permission(perm(MODEL, VIEW)))],
)
async def price_policy() -> dict[str, bool]:
    """Whether this tenant may edit prices, so the UI can hide the controls."""
    return {"readOnly": get_settings().model_prices_read_only}


@router.get(
    "/model-prices/history",
    response_model=list[ModelPriceDTO],
    dependencies=[Depends(require_permission(perm(MODEL, VIEW)))],
)
async def price_history(
    db: DbSession,
    provider: str,
    model_pattern: Annotated[str, Query(alias="modelPattern")],
) -> list[ModelPriceDTO]:
    rows = (
        (
            await db.execute(
                select(m.ModelPrice)
                .where(
                    m.ModelPrice.provider == provider, m.ModelPrice.model_pattern == model_pattern
                )
                .order_by(m.ModelPrice.effective_from.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_dto(r) for r in rows]


@router.post(
    "/model-prices",
    response_model=ModelPriceDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE))), Depends(_require_writable)],
)
async def create_price_version(body: ModelPriceWrite, db: DbSession) -> ModelPriceDTO:
    # `created_by` is purely informational (spec: never required for
    # correctness). Principal.subject is an opaque string, not necessarily a
    # UUID (see oc8.auth.principal.Principal), so it cannot be written into
    # this UUID column; left unset until callers carry a real actor id.
    row = m.ModelPrice(
        provider=body.provider,
        model_pattern=body.model_pattern,
        price_in_usd_per_1m=body.price_in_usd_per_1m,
        price_out_usd_per_1m=body.price_out_usd_per_1m,
    )
    db.add(row)
    await db.flush()
    return _to_dto(row)


@router.delete(
    "/model-prices/{price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE))), Depends(_require_writable)],
)
async def deactivate_price(price_id: uuid.UUID, db: DbSession) -> None:
    row = await db.get(m.ModelPrice, price_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "price not found")
    new_row = m.ModelPrice(
        provider=row.provider,
        model_pattern=row.model_pattern,
        price_in_usd_per_1m=row.price_in_usd_per_1m,
        price_out_usd_per_1m=row.price_out_usd_per_1m,
        active=False,
    )
    db.add(new_row)
    await db.flush()
