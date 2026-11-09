"""GET /reports -- finished runs that produced at least one rendered
component (a chart, a table, a record card via `render_component`),
scoped to the caller's visible agents. This is what surfaces a "daily
report agent"'s output somewhere besides that one run's own Live Log --
the durable `rendered_components` persistence (engine.py/internal_agent.py,
threaded through `RunDTO`) is what makes this possible for both an
autonomous cron run and a chat turn."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.repo import resolve_run_reach, restrict_to_run_reach, visible_agents
from oc8.api.deps import DbSession, require_departmental
from oc8.authz.authority import authority_for_principal, tenant_wide_read
from oc8.authz.permissions import AGENT, RUN, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.schemas.dto import ReportDTO

router = APIRouter()


@router.get("/reports", response_model=list[ReportDTO])
async def list_reports(
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
    agent_id: Annotated[uuid.UUID | None, Query(alias="agentId")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ReportDTO]:
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW))
    # Same scoped-repository funnel `/agents` itself reads through -- a report
    # never surfaces an agent the caller could not otherwise open.
    agents, _total = await visible_agents(db, scope=actor.scope, tenant_wide=tenant_wide)
    agent_ids = {a.id for a in agents}
    if agent_id is not None:
        agent_ids &= {agent_id}
    if not agent_ids:
        return []
    agent_names = {a.id: a.name for a in agents}

    # `jsonb_array_length` on a missing/NULL key evaluates to SQL NULL, and
    # `NULL > 0` is false -- runs from before this feature (no
    # rendered_components key at all) are excluded without a separate
    # "key exists" check.
    has_components = func.jsonb_array_length(m.AgentRun.context["rendered_components"]) > 0
    # And the run rule on top: a report IS run output, so it follows
    # `GET /runs` -- a seat that may see the agent but holds no `run:view`
    # gets none. Intersected in the statement, before the limit.
    reach = await resolve_run_reach(request, db, actor, perm(RUN, VIEW))
    stmt = restrict_to_run_reach(
        select(m.AgentRun)
        .where(
            m.AgentRun.tenant_id == actor.principal.tenant_id,
            m.AgentRun.agent_id.in_(agent_ids),
            m.AgentRun.state == "done",
            has_components,
        )
        .order_by(m.AgentRun.updated_at.desc())
        .limit(limit),
        m.AgentRun.agent_id,
        reach,
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        ReportDTO(
            run_id=str(run.id),
            agent_id=str(run.agent_id),
            agent_name=agent_names.get(run.agent_id, ""),
            created_at=run.updated_at.isoformat(),
            rendered_components=run.context.get("rendered_components", []),
        )
        for run in rows
    ]
