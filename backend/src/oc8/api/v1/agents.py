"""Agent read endpoints (list + detail with effective permissions).

Both routes are `require_departmental(perm(AGENT, VIEW))` -- READ graduates for
any live seat, with no toggle (decision 2 of the department-scoped-agent-
authority design): `agent:view` joined `SEAT_PERMISSIONS`'s view level for both
seat roles, so the existing, unmodified `require_departmental` gate is enough;
no new gate shape was needed for the read side. Both bodies go through
`agents.repo.visible_agent(s)` -- the scoped repository funnel -- rather than a
raw `select(m.Agent)`, which is what makes `tests/agents/test_reads_go_
through_the_scoped_repository.py`'s sweep pass for this module: a screen that
reads an `Agent` outside this file has to walk around the funnel to do it, and
that sweep is what makes doing so a decision instead of a typo.

`tenant_wide` is resolved via `authz.authority.tenant_wide_read`, not by testing
`perm(AGENT, VIEW) in authority.tenant_wide` directly: that string is now
DELEGATABLE (a tenant-defined role may hold it), and a role assignment must
never widen a caller past `scope.viewable` -- `tenant_wide_read` admits only a
genuinely tenant-wide grant (the token floor, or an explicitly assigned
`builtin=True` role), the same "company-wide role, not a composed one" fact
`actor.scope.is_unrestricted` (`approval:view_any`, a different grant entirely)
answers for approvals.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.repo import visible_agent, visible_agents
from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1._serializers import agent_to_dto, agent_version_to_summary_dto
from oc8.api.v1.files import _attachment_dto
from oc8.authz.authority import authority_for_principal, tenant_wide_read
from oc8.authz.pdp import ToolPolicy, effective_tool_policies, tool_policy_source
from oc8.authz.permissions import AGENT, AGENT_VERSION, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.runtime.states import TERMINAL
from oc8.schemas.dto import (
    AgentDetailDTO,
    AgentDTO,
    AgentInstructionHistoryDTO,
    AgentInstructionRevisionDTO,
    AgentVersionSummaryDTO,
    FileAttachmentDTO,
    ToolPolicyDTO,
)
from oc8.schemas.paging import Page

router = APIRouter()


@router.get(
    "/agents",
    response_model=Page[AgentDTO],
)
async def list_agents(
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
    # `Query(alias="departmentId")`: every other query/body field on the wire is
    # camelCase (`CamelModel`'s convention), and a bare `department_id` here
    # silently accepted nothing, so `?departmentId=` was intersecting against
    # `None` -- the caller's own scope, unfiltered -- rather than the requested
    # department. Caught by `tests/api/test_department_reads_department_scoped.py`.
    department_id: Annotated[uuid.UUID | None, Query(alias="departmentId")] = None,
    search: str | None = None,
    status: str | None = None,
    group_by: Annotated[str | None, Query(alias="groupBy")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_archived: Annotated[bool, Query(alias="includeArchived")] = False,
) -> Page[AgentDTO]:
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW))
    rows, total = await visible_agents(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide,
        department_id=department_id,
        search=search,
        status=status,
        group_by=group_by,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
    )
    return Page(items=[agent_to_dto(a) for a in rows], total_count=total)


@router.get(
    "/agents/{agent_id}",
    response_model=AgentDetailDTO,
)
async def get_agent(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
) -> AgentDetailDTO:
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW))
    agent = await visible_agent(db, scope=actor.scope, tenant_wide=tenant_wide, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return await _agent_detail_dto(db, agent)


_HISTORY_MAX_LIMIT = 100


@router.get(
    "/agents/{agent_id}/instructions/history",
    response_model=AgentInstructionHistoryDTO,
)
async def get_agent_instruction_history(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
    before_seq: Annotated[int | None, Query(alias="beforeSeq")] = None,
    limit: int = 20,
) -> AgentInstructionHistoryDTO:
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW))
    agent = await visible_agent(db, scope=actor.scope, tenant_wide=tenant_wide, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    limit = max(1, min(limit, _HISTORY_MAX_LIMIT))
    where = (
        m.AuditEvent.action == "agent.instructions.updated",
        m.AuditEvent.resource["agent_id"].astext == str(agent_id),
    )
    # A separate COUNT, not len(rows) -- the panel numbers versions by their
    # true position (vN downwards) even when only one page is loaded, and
    # that needs the total regardless of how much of it is on this page.
    total_count = (
        await db.execute(select(func.count()).select_from(m.AuditEvent).where(*where))
    ).scalar_one()
    stmt = select(m.AuditEvent).where(*where)
    if before_seq is not None:
        stmt = stmt.where(m.AuditEvent.seq < before_seq)
    rows = (
        (await db.execute(stmt.order_by(m.AuditEvent.seq.desc()).limit(limit + 1))).scalars().all()
    )
    page = rows[:limit]
    next_before_seq = page[-1].seq if len(rows) > limit and page else None
    return AgentInstructionHistoryDTO(
        revisions=[
            AgentInstructionRevisionDTO(
                ts=ev.ts.isoformat(),
                before=str(ev.resource.get("before", "")),
                after=str(ev.resource.get("after", "")),
                by=ev.resource.get("by"),
            )
            for ev in page
        ],
        total_count=total_count,
        next_before_seq=next_before_seq,
    )


@router.get(
    "/agents/{agent_id}/instruction-files",
    response_model=list[FileAttachmentDTO],
)
async def list_instruction_files(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
) -> list[FileAttachmentDTO]:
    """Files attached to this agent's standing Instructions
    (`FileAttachment(owner_type="agent_instructions")`, Task 11) -- gated the
    same view-level way as this file's other read routes, not write-narrowed:
    the upload endpoint (`agents_write.py::upload_instruction_file`) is a
    mutation and belongs behind `authorize_agent_write`, but this is a plain
    list read, and `files.py`'s own `_owned_attachment` already applies this
    exact `visible_agent` check to GET/DELETE `/files/{id}` for the same
    owner type -- a caller who can already download or delete an individual
    file by id must not get a 403 for merely listing them."""
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW))
    agent = await visible_agent(db, scope=actor.scope, tenant_wide=tenant_wide, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    rows = (
        (
            await db.execute(
                select(m.FileAttachment)
                .where(
                    m.FileAttachment.tenant_id == actor.principal.tenant_id,
                    m.FileAttachment.owner_type == "agent_instructions",
                    m.FileAttachment.owner_id == agent.id,
                )
                .order_by(m.FileAttachment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_attachment_dto(row) for row in rows]


#: Same ceiling as the instruction history above, and for the same reason: a
#: tenant that has published a thousand versions of one agent must not be able
#: to ask for all of them in one request.
_VERSIONS_MAX_LIMIT = 100


@router.get(
    "/agents/{agent_id}/versions",
    response_model=Page[AgentVersionSummaryDTO],
)
async def list_agent_versions(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT_VERSION, VIEW)))],
    limit: Annotated[int, Query(ge=1, le=_VERSIONS_MAX_LIMIT)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AgentVersionSummaryDTO]:
    """Every published version of this agent, newest first (spec §4).

    `require_departmental` rather than `require_permission` even though no seat
    carries `agent_version:view`: the gate's job here is to resolve the
    `HumanActor` whose `scope` `visible_agent` needs, and using the departmental
    shape leaves the door open for a later slice that DOES put this string in
    `SEAT_PERMISSIONS` without rewriting the route. Today the practical effect
    is tenant-wide-only, and a seat-only caller (`dept_viewer`) gets a 403 --
    which is the intended asymmetry, not an oversight: spec §6 makes reading
    version history its own grant, and `SEAT_PERMISSIONS` is closed at four
    strings by `tests/authz/test_seat_vocabulary.py`.

    `tenant_wide_read` is asked about THIS route's permission, not about
    `agent:view`: those are different grants and a caller may hold either
    without the other.
    """
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(AGENT_VERSION, VIEW))
    agent = await visible_agent(db, scope=actor.scope, tenant_wide=tenant_wide, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

    # `agent_version` carries no foreign key (house convention -- there are two
    # `ForeignKey()` declarations in the whole models package), so this
    # predicate is the ONLY thing scoping the query to this agent. RLS scopes it
    # to the tenant; nothing scopes it to the row but this.
    where = (m.AgentVersion.agent_id == agent.id,)
    total_count = (
        await db.execute(select(func.count()).select_from(m.AgentVersion).where(*where))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(m.AgentVersion)
                .where(*where)
                .order_by(m.AgentVersion.version_no.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return Page(
        items=[
            agent_version_to_summary_dto(row, current_version_id=agent.current_version_id)
            for row in rows
        ],
        total_count=total_count,
    )


async def _agent_detail_dto(db: DbSession, agent: m.Agent) -> AgentDetailDTO:
    """The DTO body for an agent already loaded and already authorized.

    Split out of `get_agent` so `agents_write.py`'s four internal re-serializes
    (after a write this same request just made) can call this directly instead
    of re-running the read gate on an agent the write gate already authorized --
    `require_departmental` would need a fresh `HumanActor`/`Request` shape
    `agents_write.py`'s routes have no reason to carry twice, and the agent is
    already known-visible: the write that just happened proved it.
    """
    dept = await db.get(m.Department, agent.department_id)
    frame = dept.frame if dept else {}
    dept_name = dept.name if dept else None

    effective = effective_tool_policies(frame, agent.narrowing)
    overridden_keys = frozenset(agent.narrowing_overridden_keys or [])
    tool_policy_sources = {
        k: tool_policy_source(
            k,
            frame=frame,
            capa_defaults=dept.frame_capa_defaults if dept else None,
            narrowing_overridden_keys=overridden_keys,
        )
        for k in effective
    }
    # Through `ToolPolicy.from_json(v).to_json()`, not a raw `**v` passthrough:
    # a stored frame row can predate migration 0087's write/send -> modify data
    # merge (Task 3) and still carry the old keys, which `from_json` no longer
    # recognizes -- normalizing here is what keeps this from raising on an
    # unmigrated row instead of just showing modify=False for it until that
    # migration runs.
    frame_tools = {
        k: ToolPolicyDTO(**ToolPolicy.from_json(v).to_json())
        for k, v in _frame_tools_json(frame).items()
    }
    # Same normalization `narrowing_within_frame`/`effective_tool_policies`
    # already apply to this same raw dict -- a stored narrowing row can be
    # partial (only the keys a given save actually touched), so this must
    # default missing read/modify the same way, not require them present.
    raw_narrowing_tools = (agent.narrowing or {}).get("tools", {})
    narrowing_tools = {
        k: ToolPolicyDTO(**ToolPolicy.from_json(v).to_json())
        for k, v in raw_narrowing_tools.items()
        if isinstance(v, dict)
    }
    base = agent_to_dto(agent).model_dump(by_alias=False)
    # Same key `resolve_params` (modelrouter/sampling.py) reads as the
    # narrowest-first sampling source -- unset here means "inherit the
    # assigned ModelConfig's own value", not "use a framework default".
    model_params = (agent.definition or {}).get("model_params") or {}
    # The newest run that has not finished. Newest, because a run abandoned by a
    # dead worker can sit in `running` indefinitely and the one worth watching is
    # the latest; queued counts, so a scheduled fire is visible before its
    # container is even up.
    current_run = (
        await db.execute(
            select(m.AgentRun.id)
            .where(
                m.AgentRun.agent_id == agent.id,
                m.AgentRun.state.notin_([s.value for s in TERMINAL]),
            )
            # id breaks the tie: created_at is the TRANSACTION time in
            # Postgres, so two runs enqueued together share it exactly. The ids
            # are time-ordered (uuid7), so this stays "newest" either way.
            .order_by(m.AgentRun.created_at.desc(), m.AgentRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return AgentDetailDTO(
        **base,
        mission=agent.mission,
        department_name=dept_name,
        effective_tools={k: ToolPolicyDTO(**p.to_json()) for k, p in effective.items()},
        department_frame_tools=frame_tools,
        narrowing_tools=narrowing_tools,
        narrowing_overridden_keys=list(agent.narrowing_overridden_keys or []),
        tool_policy_sources=tool_policy_sources,
        runtime_ref=agent.runtime_ref,
        current_run_id=str(current_run) if current_run else None,
        temperature=model_params.get("temperature"),
        max_tokens=model_params.get("max_tokens"),
        effort=model_params.get("effort"),
        extra=model_params.get("extra") if isinstance(model_params.get("extra"), dict) else None,
        # Top-level key, unlike the four sampling fields above -- see
        # engine._max_steps and switch_model in agents_write.py.
        max_steps=(agent.definition or {}).get("max_steps"),
    )


def _frame_tools_json(frame: dict[str, object]) -> dict[str, dict[str, object]]:
    tools = frame.get("tools", {}) if frame else {}
    return tools if isinstance(tools, dict) else {}
