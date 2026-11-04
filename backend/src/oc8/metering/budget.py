"""Per-tenant / per-department token budgets (§15.4)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.approvals.service import raise_approval
from oc8.audit import append_event
from oc8.metering.pricing import active_price_rows, price_as_of
from oc8.models.core import Agent, ModelConfig
from oc8.models.ops import ApprovalRequest, Budget, TokenUsageRecord
from oc8.models.run import AgentRun, RunCancellation
from oc8.realtime.bus import get_event_bus
from oc8.realtime.emit import publish_agent_status


async def set_budget(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID | None,
    soft_limit_tokens: int | None,
    hard_limit_tokens: int | None,
) -> Budget:
    existing = await get_budget(db, tenant_id=tenant_id, department_id=department_id)
    if existing is not None:
        existing.soft_limit_tokens = soft_limit_tokens
        existing.hard_limit_tokens = hard_limit_tokens
        await db.flush()
        await resume_budget_scope(db, tenant_id=tenant_id, department_id=department_id)
        return existing
    budget = Budget(
        tenant_id=tenant_id,
        department_id=department_id,
        soft_limit_tokens=soft_limit_tokens,
        hard_limit_tokens=hard_limit_tokens,
    )
    db.add(budget)
    await db.flush()
    await resume_budget_scope(db, tenant_id=tenant_id, department_id=department_id)
    return budget


async def convert_dollars_to_tokens(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID | None, dollar_amount: float
) -> tuple[int, str, str]:
    """$ -> tokens, once, at write time. Returns (tokens, provider, model) --
    the reference model used, so the caller can store it on Budget for
    display. Reference-model resolution: (1) the model with the most tokens
    recorded in this scope over the trailing 30 days (tenant-wide when
    department_id is None, else that department only), (2) the tenant's
    Copilot-flagged ModelConfig if the scope has no usage yet, (3) raise
    ValueError -- never silently guess a reference model."""
    since = datetime.now(tz=UTC) - timedelta(days=30)
    total = func.sum(TokenUsageRecord.tokens_in + TokenUsageRecord.tokens_out)
    query = select(TokenUsageRecord.provider, TokenUsageRecord.model, total.label("total")).where(
        TokenUsageRecord.tenant_id == tenant_id, TokenUsageRecord.ts >= since
    )
    if department_id is not None:
        query = query.where(TokenUsageRecord.department_id == department_id)
    query = (
        query.group_by(TokenUsageRecord.provider, TokenUsageRecord.model)
        .order_by(total.desc())
        .limit(1)
    )
    row = (await db.execute(query)).first()

    if row is not None:
        provider, model = row[0], row[1]
    else:
        copilot = (
            await db.execute(
                select(ModelConfig).where(
                    ModelConfig.tenant_id == tenant_id, ModelConfig.used_by_copilot.is_(True)
                )
            )
        ).scalar_one_or_none()
        if copilot is None:
            raise ValueError(
                "no reference model: this scope has no recorded usage and no Copilot model is set"
            )
        provider, model = copilot.provider, copilot.model

    now = datetime.now(tz=UTC)
    price_rows = await active_price_rows(db, as_of=now)
    price = price_as_of(price_rows, provider, model, now)
    if price is None:
        raise ValueError(f"no reference model: {provider}/{model} has no price on record")
    blended_usd_per_1m = (price[0] + price[1]) / 2
    tokens = round(dollar_amount * 1_000_000 / blended_usd_per_1m)
    return tokens, provider, model


async def get_budget(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID | None
) -> Budget | None:
    return (
        await db.execute(
            select(Budget).where(
                Budget.tenant_id == tenant_id, Budget.department_id == department_id
            )
        )
    ).scalar_one_or_none()


async def list_budgets(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[Budget]:
    return list(
        (await db.execute(select(Budget).where(Budget.tenant_id == tenant_id))).scalars().all()
    )


def _month_start() -> datetime:
    now = datetime.now(tz=UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start() -> datetime:
    now = datetime.now(tz=UTC)
    if now.month == 12:
        return now.replace(
            year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


async def current_month_tokens(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID | None
) -> int:
    """Sum tokens_in+tokens_out since the start of the current calendar
    month. department_id=None sums across the whole tenant."""
    filters = [TokenUsageRecord.tenant_id == tenant_id, TokenUsageRecord.ts >= _month_start()]
    if department_id is not None:
        filters.append(TokenUsageRecord.department_id == department_id)
    result = await db.execute(
        select(
            func.coalesce(func.sum(TokenUsageRecord.tokens_in + TokenUsageRecord.tokens_out), 0)
        ).where(*filters)
    )
    return int(result.scalar_one())


@dataclass
class BudgetCheck:
    """The ALLOW/DENY decision run_agent needs. Not a display model — see
    oc8.api.v1.budgets for the richer status view built directly from
    get_budget()/current_month_tokens()."""

    soft_exceeded: bool
    hard_exceeded: bool


async def check_budget(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID | None
) -> BudgetCheck:
    """Evaluates both the tenant-wide budget and the department's own budget
    (if configured); either's hard limit being exceeded makes the whole
    check hard_exceeded -- whichever scope is stricter governs.
    `department_id=None` checks the tenant-wide budget only (work that belongs
    to no department, such as embedding a knowledge base)."""
    soft_exceeded = False
    hard_exceeded = False
    now = datetime.now(tz=UTC)
    for scope_department_id in dict.fromkeys((None, department_id)):
        budget = await get_budget(db, tenant_id=tenant_id, department_id=scope_department_id)
        if budget is None:
            continue
        usage = await current_month_tokens(
            db, tenant_id=tenant_id, department_id=scope_department_id
        )
        override_active = budget.override_until is not None and now < budget.override_until
        if budget.soft_limit_tokens is not None and usage >= budget.soft_limit_tokens:
            soft_exceeded = True
        if (
            not override_active
            and budget.hard_limit_tokens is not None
            and usage >= budget.hard_limit_tokens
        ):
            hard_exceeded = True
    return BudgetCheck(soft_exceeded=soft_exceeded, hard_exceeded=hard_exceeded)


async def resume_budget_scope(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID | None
) -> int:
    """Clear budget-pauses on a scope (department_id=None => the whole tenant).
    Resumes ONLY agents paused for budget (pause_reason=='budget') AND only when
    that agent's scope is no longer hard-exceeded (so a still-over agent stays
    paused, and a supervision-pause is never touched). Returns the count resumed.
    add/flush only -- never commits."""
    stmt = select(Agent).where(
        Agent.tenant_id == tenant_id,
        Agent.status == "paused",
        Agent.pause_reason == "budget",
    )
    if department_id is not None:
        stmt = stmt.where(Agent.department_id == department_id)
    resumed = 0
    resumed_agents: list[Agent] = []
    for agent in (await db.execute(stmt)).scalars().all():
        check = await check_budget(db, tenant_id=tenant_id, department_id=agent.department_id)
        if not check.hard_exceeded:
            agent.status = "idle"
            agent.pause_reason = None
            agent.paused_at = None
            resumed += 1
            resumed_agents.append(agent)
    await db.flush()
    for agent in resumed_agents:
        await publish_agent_status(agent)
    return resumed


async def trigger_budget_hard_stop(
    db: AsyncSession, *, tenant_id: uuid.UUID, breaching_agent: Agent
) -> ApprovalRequest | None:
    """Freeze the breaching scope on a hard budget breach (§15.4 A2): pause the
    scope's idle agents (running ones self-heal via the per-run check on their
    next run -- avoids the executor's unconditional idle-on-terminal clobber),
    cancel the scope's queued runs (§7.2 run_cancellation rows), and raise ONE
    budget_incident approval. add/flush only -- NEVER commits (runs inside
    execute_run's still-bound tenant_session). Returns the incident, or None when
    the scope was already frozen (idempotent, no duplicate incident)."""
    now = datetime.now(tz=UTC)

    # Scope: tenant-wide dominates the department.
    scope_department_id: uuid.UUID | None = breaching_agent.department_id
    tw = await get_budget(db, tenant_id=tenant_id, department_id=None)
    if tw is not None and tw.hard_limit_tokens is not None:
        tw_override = tw.override_until is not None and now < tw.override_until
        tw_usage = await current_month_tokens(db, tenant_id=tenant_id, department_id=None)
        if not tw_override and tw_usage >= tw.hard_limit_tokens:
            scope_department_id = None

    # The breaching agent is always frozen (it is the one over budget).
    breaching_agent.status = "paused"
    breaching_agent.pause_reason = "budget"
    breaching_agent.paused_at = now
    await db.flush()
    await publish_agent_status(breaching_agent)

    # Idempotency: is there already a PENDING budget_incident for THIS scope?
    # Key on the incident, NOT on agent pause-state: a tenant-wide freeze must not
    # be suppressed by agents an earlier DEPARTMENT freeze paused (scope bleed),
    # and a stale pause_reason can never falsely suppress a fresh incident. The
    # payload's department_id (a UUID string, or JSON null for tenant-wide)
    # identifies the scope exactly.
    already_stmt = select(ApprovalRequest.id).where(
        ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.action_type == "budget_incident",
        ApprovalRequest.status == "pending",
    )
    if scope_department_id is not None:
        already_stmt = already_stmt.where(
            ApprovalRequest.payload["department_id"].astext == str(scope_department_id)
        )
    else:
        already_stmt = already_stmt.where(ApprovalRequest.payload["department_id"].astext.is_(None))
    already_frozen = (await db.execute(already_stmt.limit(1))).first() is not None

    # Freeze the scope's IDLE agents (running ones re-freeze on their next run).
    freeze_stmt = select(Agent).where(Agent.tenant_id == tenant_id, Agent.status == "idle")
    if scope_department_id is not None:
        freeze_stmt = freeze_stmt.where(Agent.department_id == scope_department_id)
    frozen_agents: list[Agent] = []
    for agent in (await db.execute(freeze_stmt)).scalars().all():
        agent.status = "paused"
        agent.pause_reason = "budget"
        agent.paused_at = now
        frozen_agents.append(agent)
    await db.flush()
    for agent in frozen_agents:
        await publish_agent_status(agent)

    # Cancel the scope's queued runs (§7.2 run_cancellation; executor skips them).
    scope_ids_stmt = select(Agent.id).where(Agent.tenant_id == tenant_id)
    if scope_department_id is not None:
        scope_ids_stmt = scope_ids_stmt.where(Agent.department_id == scope_department_id)
    scope_agent_ids = list((await db.execute(scope_ids_stmt)).scalars().all())
    queued_run_ids = list(
        (
            await db.execute(
                select(AgentRun.id).where(
                    AgentRun.state == "queued", AgentRun.agent_id.in_(scope_agent_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    for run_id in queued_run_ids:
        exists = (
            await db.execute(select(RunCancellation.id).where(RunCancellation.run_id == run_id))
        ).scalar_one_or_none()
        if exists is None:
            db.add(
                RunCancellation(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    requested_at=now,
                    cancellation_kind="budget_hard_stop",
                )
            )

    scope_label = "tenant" if scope_department_id is None else "department"
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="agent",
        actor_id=breaching_agent.id,
        category="budget",
        action="budget_hard_stop",
        resource={
            "scope": scope_label,
            "department_id": str(scope_department_id) if scope_department_id else None,
            "queued_cancelled": len(queued_run_ids),
        },
        decision="deny",
        reason="token budget hard-exceeded",
    )

    if already_frozen:
        return None

    usage = await current_month_tokens(db, tenant_id=tenant_id, department_id=scope_department_id)
    scope_budget = await get_budget(db, tenant_id=tenant_id, department_id=scope_department_id)
    hard_limit = scope_budget.hard_limit_tokens if scope_budget is not None else None
    # Through the funnel, not `ApprovalRequest(...)` by hand. Two things were
    # missing while this row was built here: it was never announced on a
    # messenger (the one approval in the system nobody is watching a screen for),
    # and once approvals carried a department it would have been born with a NULL
    # one whatever the scope -- so a DEPARTMENT budget breach would have appeared
    # only in the CEO's queue, not the queue of the department that spent it.
    #
    # This is the sole caller allowed to pass `department_id`, and
    # `tests/approvals/test_department_is_pinned_at_raise.py` asserts that by
    # sweeping the source. A tenant-scope breach is genuinely company-wide:
    # `scope_department_id` is None there, and filing it under the breaching
    # agent's department would hide the company's problem inside one team's queue.
    incident = await raise_approval(
        db,
        tenant_id=tenant_id,
        agent_id=breaching_agent.id,
        department_id=scope_department_id,
        action_type="budget_incident",
        payload={
            "scope": scope_label,
            "department_id": str(scope_department_id) if scope_department_id else None,
            "usage_tokens": usage,
            "hard_limit_tokens": hard_limit,
        },
        title=f"Token budget exceeded — {scope_label}",
        detail=(
            "Runs are paused and queued work was cancelled. Approve to allow spend "
            "for the rest of this budget window, or reject to keep the scope paused."
        ),
        amount_text=(
            f"{usage} / {hard_limit} tokens" if hard_limit is not None else f"{usage} tokens"
        ),
    )

    await get_event_bus().publish_event(
        incident.tenant_id,
        "approval.created",
        {
            "approval_id": str(incident.id),
            "action_type": incident.action_type,
            "status": incident.status,
            # Carried in the envelope on purpose: the caller commits, not us,
            # so a fresh session reading this row would see nothing (see
            # EventBus._push_payload).
            "title": incident.title,
            "detail": incident.detail,
        },
        source=f"oc8/approval/{incident.id}",
    )
    return incident


async def resolve_budget_incident(
    db: AsyncSession, *, approval_request: ApprovalRequest, decision: str
) -> None:
    """Resolve a budget_incident (§15.4 A2). approve => grant a one-time window
    override on the scope budget + resume the scope; reject => leave it paused
    (recover via PUT /budgets or a month reset). add/flush only -- never commits;
    the decision endpoint owns the commit."""
    if decision != "approve":
        return
    payload = approval_request.payload or {}
    dep = payload.get("department_id")
    scope_department_id = uuid.UUID(str(dep)) if dep else None
    tenant_id = approval_request.tenant_id
    budget = await get_budget(db, tenant_id=tenant_id, department_id=scope_department_id)
    if budget is not None:
        budget.override_until = _next_month_start()
    await resume_budget_scope(db, tenant_id=tenant_id, department_id=scope_department_id)
