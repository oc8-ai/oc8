# backend/src/oc8/api/v1/budgets.py
"""Per-tenant / per-department token budgets (§15.4)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import BUDGET, MANAGE, VIEW, perm
from oc8.departments.personal import stamp_budget_override
from oc8.metering.budget import (
    convert_dollars_to_tokens,
    current_month_tokens,
    get_budget,
    list_budgets,
    set_budget,
)
from oc8.schemas.dto import BudgetDTO, BudgetStatusDTO
from oc8.schemas.requests import SetBudgetRequest

router = APIRouter()


def _to_dto(b: m.Budget) -> BudgetDTO:
    return BudgetDTO(
        id=str(b.id),
        department_id=str(b.department_id) if b.department_id else None,
        soft_limit_tokens=b.soft_limit_tokens,
        hard_limit_tokens=b.hard_limit_tokens,
        dollar_budget_usd=float(b.dollar_budget_usd) if b.dollar_budget_usd is not None else None,
        dollar_reference_provider=b.dollar_reference_provider,
        dollar_reference_model=b.dollar_reference_model,
    )


@router.put(
    "/budgets",
    response_model=BudgetDTO,
    dependencies=[Depends(require_permission(perm(BUDGET, MANAGE)))],
)
async def upsert_budget(
    body: SetBudgetRequest, db: DbSession, principal: CurrentPrincipal
) -> BudgetDTO:
    soft = body.soft_limit_tokens
    hard = body.hard_limit_tokens
    dollar_provider = dollar_model = None
    if body.dollar_budget_usd is not None:
        try:
            hard, dollar_provider, dollar_model = await convert_dollars_to_tokens(
                db,
                tenant_id=principal.tenant_id,
                department_id=body.department_id,
                dollar_amount=body.dollar_budget_usd,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        soft = None
    budget = await set_budget(
        db,
        tenant_id=principal.tenant_id,
        department_id=body.department_id,
        soft_limit_tokens=soft,
        hard_limit_tokens=hard,
    )
    if body.department_id is not None:
        await stamp_budget_override(
            db, tenant_id=principal.tenant_id, department_id=body.department_id
        )
    if body.dollar_budget_usd is not None:
        budget.dollar_budget_usd = body.dollar_budget_usd
        budget.dollar_reference_provider = dollar_provider
        budget.dollar_reference_model = dollar_model
    else:
        # Token mode was explicitly chosen -- clear any stale $ fields from a
        # previous $ save, otherwise the frontend's `dollarBudgetUsd != null`
        # mode-detection snaps back to showing the old (now-wrong) $ amount.
        budget.dollar_budget_usd = None
        budget.dollar_reference_provider = None
        budget.dollar_reference_model = None
    await db.flush()
    return _to_dto(budget)


@router.get(
    "/budgets",
    response_model=list[BudgetDTO],
    dependencies=[Depends(require_permission(perm(BUDGET, VIEW)))],
)
async def get_budgets(db: DbSession, principal: CurrentPrincipal) -> list[BudgetDTO]:
    rows = await list_budgets(db, tenant_id=principal.tenant_id)
    return [_to_dto(b) for b in rows]


@router.get(
    "/budgets/status",
    response_model=BudgetStatusDTO,
    dependencies=[Depends(require_permission(perm(BUDGET, VIEW)))],
)
async def budget_status(
    db: DbSession,
    principal: CurrentPrincipal,
    department_id: uuid.UUID | None = None,
) -> BudgetStatusDTO:
    budget = await get_budget(db, tenant_id=principal.tenant_id, department_id=department_id)
    usage = await current_month_tokens(
        db, tenant_id=principal.tenant_id, department_id=department_id
    )
    soft = budget.soft_limit_tokens if budget is not None else None
    hard = budget.hard_limit_tokens if budget is not None else None
    return BudgetStatusDTO(
        scope="department" if department_id is not None else "tenant",
        department_id=str(department_id) if department_id is not None else None,
        soft_limit_tokens=soft,
        hard_limit_tokens=hard,
        current_tokens=usage,
        soft_exceeded=soft is not None and usage >= soft,
        hard_exceeded=hard is not None and usage >= hard,
    )
