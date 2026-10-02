"""Departments, their agents, and task boards.

The five `GET` routes are `require_departmental(perm(DEPARTMENT, VIEW))` --
READ graduates for any live seat, with no toggle (decision 2 of the
department-scoped-agent-authority design). `PUT /departments/{id}/tools` is
UNCHANGED -- `department:manage`'s broader surface is out of scope for this
slice, so it stays on the tenant-wide-only `require_permission` gate it always
had.

`GET /departments/{id}/tools` shares the identical `department:view`
permission string with the detail/board/agents routes on purpose: forking it
would make `department:view` mean two different things depending on which
route asked, exactly the "door and filter disagree" failure `require_
departmental`'s own docstring warns against. No new information leaks either
way -- a seat holder who can see an agent already sees the identical frame via
`department_frame_tools` on the agent-detail route.

`department_agents`/`department_board` check the department's own visibility
FIRST (`_visible_department_or_404`) so a foreign or bogus `dept_id` 404s
consistently, same as `get_department` -- before this design both returned an
empty 200 for either reason, indistinguishable from "this department exists
and has nothing in it".

`tenant_wide` is resolved via `authz.authority.tenant_wide_read`, never by
testing `perm(DEPARTMENT, VIEW) in authority.tenant_wide` directly -- that
string is DELEGATABLE, and a tenant-defined role holding it must never widen a
caller past `scope.viewable`. See `tenant_wide_read`'s docstring and
`agents.py`'s identical note; this module and that one share the exact same
bug class and the exact same fix.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ConfigDict, Field, field_validator
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.publish_hooks import PublishHookFailed
from oc8.agents.repo import visible_agents
from oc8.agents.versioning import NoChangesToPublish, draft_status, publish_version
from oc8.api.deps import CurrentPrincipal, DbSession, require_departmental, require_permission
from oc8.api.v1._serializers import agent_to_dto, department_to_dto, task_to_dto
from oc8.audit import append_event
from oc8.authz.authority import authority_for_principal, tenant_wide_read
from oc8.authz.pdp import ConditionDatatype, ConditionOperator, Effect
from oc8.authz.permissions import DEPARTMENT, MANAGE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.capas.discovery import connection_supports_value_spec, resolve_tool_pack_connection
from oc8.departments.repo import visible_department, visible_departments
from oc8.schemas.base import CamelModel
from oc8.schemas.dto import AgentDTO, BoardDTO, DepartmentDTO, TaskDTO
from oc8.schemas.paging import Page

logger = logging.getLogger(__name__)

router = APIRouter()


class DepartmentToolsDTO(CamelModel):
    tools: dict[str, dict[str, Any]] = {}
    #: tool key -> count of agents in this department whose
    #: narrowing_overridden_keys contains that key -- the frontend's "N of M
    #: agents deviate" display. Computed fresh on every read; not stored.
    deviation_counts: dict[str, int] = {}
    #: Total agents in the department -- the "M" half of "N of M".
    agent_count: int = 0


class ConditionWriteDTO(CamelModel):
    """Write-side shape for one entry of `ToolPolicyWriteDTO.conditions`,
    mirroring `authz.pdp.Condition` field-for-field -- the generic "with
    limits" rule row (design: OC8 Guardrails UX spec, generic Condition
    model). Deliberately does NOT cross-reference `attribute` against a
    connection's declared `GuardrailAttribute`s (this endpoint has no access
    to a resolved manifest, same reason `only`/`approval_actions` above
    don't cross-reference either) -- the frontend's Conditions editor is
    what constrains attribute choice to what the connection actually
    declares; this only validates that a submitted condition is well-formed.
    """

    model_config = ConfigDict(extra="forbid")
    attribute: str
    datatype: str = ConditionDatatype.NUMBER.value
    operator: str = ConditionOperator.GTE.value
    value: Any = None
    then: str = Effect.REQUIRE_APPROVAL.value

    @field_validator("attribute")
    @classmethod
    def _attribute_is_not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("attribute must not be the empty string")
        return v

    @field_validator("datatype")
    @classmethod
    def _datatype_is_known(cls, v: str) -> str:
        if v not in {d.value for d in ConditionDatatype}:
            raise ValueError(f"unknown condition datatype: {v!r}")
        return v

    @field_validator("operator")
    @classmethod
    def _operator_is_known(cls, v: str) -> str:
        if v not in {o.value for o in ConditionOperator}:
            raise ValueError(f"unknown condition operator: {v!r}")
        return v

    @field_validator("then")
    @classmethod
    def _then_is_a_real_effect(cls, v: str) -> str:
        if v not in {e.value for e in Effect}:
            raise ValueError(f"unknown condition effect: {v!r}")
        return v


class ToolPolicyWriteDTO(CamelModel):
    """Write-side shape for one entry of `PUT /departments/{id}/tools`,
    mirroring `authz.pdp.ToolPolicy` field-for-field so a department's tool
    policy can no longer be written as arbitrary unchecked JSON.

    This validates that a payload is well-formed -- it does NOT cross-
    reference `approval_actions`/`only` entries against a connection's real
    MCP tool list (that would need this endpoint to resolve `frame["tools"]`'s
    key back to a live `McpConnection` row and its plugin manifest, which it
    has no access to). `only` already accepts hand-typed tool names with the
    same lack of cross-reference today, so `approval_actions` keeps the same
    latitude for consistency -- see task-1b-brief.md "Why this scope".
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    read: bool = False
    modify: bool = False
    approval_eur: int | None = None
    approval_actions: list[str] = []
    only: list[str] | None = None
    #: Generic "with limits" rules -- see `ConditionWriteDTO`. Additive
    #: alongside `approval_eur`/`approval_actions`, mirroring
    #: `authz.pdp.ToolPolicy.conditions` field-for-field.
    conditions: list[ConditionWriteDTO] = []
    #: The McpConnection this department's agents use for this tool when they
    #: have no narrowing pin of their own -- `_resolve_mcp_connection`
    #: (runtime/executor.py) reads it back out of `frame["tools"][key]`
    #: verbatim, so its shape here is exactly what that reader expects: a
    #: connection id (any tenant connection sharing this tool's name, not
    #: necessarily this department's own), or None for "no explicit default,
    #: fall back to the legacy department_id lookup" -- today's behaviour,
    #: unchanged for a department that never sets this.
    default_connection_id: str | None = None

    @field_validator("approval_eur")
    @classmethod
    def _threshold_is_not_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("approval_eur must not be negative")
        return v

    @field_validator("default_connection_id")
    @classmethod
    def _default_connection_id_is_a_uuid_or_absent(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError("default_connection_id must be a UUID") from exc
        return v

    @field_validator("approval_actions", "only")
    @classmethod
    def _entries_are_not_blank(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and any(entry == "" for entry in v):
            raise ValueError("entries must not be the empty string")
        return v


class DepartmentToolsWriteRequest(CamelModel):
    tools: dict[str, ToolPolicyWriteDTO] = {}


class DepartmentPatchRequest(CamelModel):
    """Partial update -- `None` means unchanged. Used by the gamified
    onboarding wizard's step 1 (design: gamified-onboarding-wizard-design.md)
    to let an admin name and theme their auto-seeded department."""

    name: str | None = Field(default=None, min_length=1)
    goal: str | None = None
    icon: str | None = None
    prompt_caching_enabled: bool | None = None


class DepartmentCreateRequest(CamelModel):
    """A blank department -- no agents, no tools, no template. The other way
    to get a department is `POST /plugins/{id}/instantiate-department`, which
    ships a department_template's pre-built agents; this is the "start empty"
    door beside it, and it's what the office view's "New department" dialog
    has always claimed to do."""

    name: str = Field(min_length=1)
    goal: str = ""
    icon: str = "building"


@router.get(
    "/departments",
    response_model=Page[DepartmentDTO],
)
async def list_departments(
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(DEPARTMENT, VIEW)))],
    search: str | None = None,
    group_by: Annotated[str | None, Query(alias="groupBy")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_archived: Annotated[bool, Query(alias="includeArchived")] = False,
) -> Page[DepartmentDTO]:
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(DEPARTMENT, VIEW))
    rows, total = await visible_departments(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide,
        search=search,
        group_by=group_by,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
    )
    return Page(items=[department_to_dto(d) for d in rows], total_count=total)


@router.post(
    "/departments",
    response_model=DepartmentDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def create_department(
    body: DepartmentCreateRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> DepartmentDTO:
    dept = m.Department(
        tenant_id=principal.tenant_id,
        name=body.name,
        goal=body.goal,
        # Same default a tenant's first (auto-provisioned) department gets --
        # an empty `memory` policy DENIES department-tier memory writes, which
        # would silently leave every agent hired into this department unable
        # to remember anything (see tenants/provision.py's own note on this).
        frame={
            "tools": {},
            "kbs": [],
            "memory": {"department": ["read", "write"], "company": ["read"]},
        },
        presentation={"icon": body.icon},
    )
    db.add(dept)
    await db.flush()
    return department_to_dto(dept)


def _deviation_counts(tools: dict[str, Any], agents: Sequence[m.Agent]) -> dict[str, int]:
    counts: dict[str, int] = {key: 0 for key in tools}
    for agent in agents:
        for key in agent.narrowing_overridden_keys or []:
            if key in counts:
                counts[key] += 1
    return counts


async def _cascade_department_tools_change(
    db: DbSession,
    *,
    old_tools: dict[str, Any],
    new_tools: dict[str, Any],
    agents: Sequence[m.Agent],
) -> None:
    """Shared by `set_department_tools` and `reset_department_tool`: cascade
    each tool's new `enabled` default to every CURRENT agent in this
    department that has never *deliberately* touched this tool key. See
    `set_department_tools`'s own long comment (unchanged, kept there rather
    than duplicated here) for why this reads `narrowing_overridden_keys` and
    never writes to it, and why the whole policy dict is written rather than
    a sparse `{"enabled": ...}` patch.

    Over the UNION of old/new keys rather than just `new_tools`, so a reset
    that removes a key from the frame entirely (no CAPA default to restore
    it to) still cascades that removal to agents who never overrode it --
    `new_tools.get(key, {})` there is an empty `ToolPolicy`, i.e. "disabled,
    no rights", the correct meaning of "this tool no longer exists here".

    Every touched agent is then PUBLISHED, because runs execute an agent's
    published version and not its row: without it, re-enabling a tool at the
    department would never reach a single agent (the frame alone cannot widen
    a pinned narrowing that says "disabled"). Only an agent with no pending
    draft is published, so the new version holds the cascade and nothing else;
    for one mid-edit, publishing would push that operator's unfinished draft
    live, so the cascade joins the draft instead and shows in its publish bar.
    A publish hook refusing rejects the whole department change (422, and
    `get_db` rolls every write back) rather than leave agents half-published.
    """
    changed_keys = {
        key
        for key in set(old_tools) | set(new_tools)
        if bool(new_tools.get(key, {}).get("enabled", False))
        != bool(old_tools.get(key, {}).get("enabled", False))
    }
    touched = [
        agent
        for agent in agents
        if any(key not in (agent.narrowing_overridden_keys or []) for key in changed_keys)
    ]
    # Read BEFORE the write below, which would make every one of them dirty.
    publishable = {agent.id for agent in touched if not (await draft_status(db, agent)).dirty}
    for key in changed_keys:
        new_policy = new_tools.get(key, {})
        for agent in agents:
            if key in (agent.narrowing_overridden_keys or []):
                continue  # a genuine, deliberate operator override -- never touch it
            narrowing = dict(agent.narrowing or {})
            agent_tools = dict(narrowing.get("tools", {}))
            agent_tools[key] = dict(new_policy)
            narrowing["tools"] = agent_tools
            agent.narrowing = narrowing
    await db.flush()
    for agent in touched:
        if agent.id not in publishable:
            logger.info(
                "department tool cascade deferred to existing draft: agent=%s",
                agent.id,
            )
            continue
        try:
            await publish_version(db, agent, note="department tool access changed")
        except NoChangesToPublish:
            # The cascade wrote what the version already had.
            continue
        except PublishHookFailed as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "error": "publish_hook_rejected",
                    "agentId": str(agent.id),
                    "hook": exc.hook_name,
                    "reason": str(exc.cause),
                },
            ) from exc


async def _get_department(db: DbSession, dept_id: uuid.UUID) -> m.Department:
    """Unscoped loader for the tenant-wide-only department mutations (`PUT
    /departments/{id}/tools`, `PATCH /departments/{id}`) -- `department:
    manage` is tenant-wide-only (no seat can ever hold it), so there is
    nothing to scope on either route. Every `GET` below uses
    `_visible_department_or_404` instead."""
    dept = await db.get(m.Department, dept_id)
    if dept is None or dept.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
    return dept


@router.patch(
    "/departments/{dept_id}",
    response_model=DepartmentDTO,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def patch_department(
    dept_id: uuid.UUID,
    body: DepartmentPatchRequest,
    db: DbSession,
    _p: CurrentPrincipal,
) -> DepartmentDTO:
    dept = await _get_department(db, dept_id)
    if body.name is not None:
        dept.name = body.name
    if body.goal is not None:
        dept.goal = body.goal
    if body.icon is not None:
        dept.presentation = {**(dept.presentation or {}), "icon": body.icon}
    if body.prompt_caching_enabled is not None:
        dept.prompt_caching_enabled = body.prompt_caching_enabled
    await db.flush()
    return department_to_dto(dept)


@router.delete(
    "/departments/{dept_id}",
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def delete_department(
    dept_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> dict[str, str]:
    """Hard-delete when nothing depends on the department; archive (soft-
    delete) otherwise -- a run/task's `department_id` must keep pointing at a
    real row (same dependents-detection shape as `agents_write.py::
    delete_agent`). A department's live dependents are its own non-archived
    agents (`Agent.department_id == this dept and deleted_at IS NULL`).

    An archived department does NOT leave its agents behind, live, pointing
    at a dead department -- every dependent agent is cascaded through the
    exact same hard-delete-or-archive decision `agents_write.py::delete_agent`
    makes for one agent on its own (by its own run history, not blanket
    archival), so an agent with no runs is actually removed and one with
    history is archived, same as calling that endpoint on it directly would
    do. This is the ONLY writer that cascades into `agent` from a department
    mutation -- `set_department_tools`'s own cascade (its docstring) only
    ever touches `agent.narrowing`, never `deleted_at`."""
    dept = await _get_department(db, dept_id)
    live_agents = (
        (
            await db.execute(
                select(m.Agent).where(
                    m.Agent.department_id == dept_id, m.Agent.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    now = dt.datetime.now(tz=dt.UTC)
    if not live_agents:
        await db.delete(dept)
        outcome = "deleted"
    else:
        dept.deleted_at = now
        outcome = "archived"
        for agent in live_agents:
            agent_dependents = (
                await db.execute(
                    select(func.count())
                    .select_from(m.AgentRun)
                    .where(m.AgentRun.agent_id == agent.id)
                )
            ).scalar_one()
            if agent_dependents == 0:
                await db.delete(agent)
                agent_outcome = "deleted"
            else:
                agent.deleted_at = now
                agent_outcome = "archived"
            await append_event(
                db,
                tenant_id=principal.tenant_id,
                actor_type="operator",
                actor_id=None,
                category="admin",
                action=f"agent.{agent_outcome}",
                resource={
                    "agent_id": str(agent.id),
                    "by": principal.subject,
                    "cascaded_from_department_id": str(dept_id),
                },
                principal=principal,
            )
    # `flush`, not `commit`: same file-wide convention as the rest of this
    # module's writes (`create_department`, `patch_department`,
    # `set_department_tools`) -- the route body never commits itself,
    # `get_db`/`tenant_session` commits once when the request finishes.
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action=f"department.{outcome}",
        resource={"department_id": str(dept_id), "by": principal.subject},
        principal=principal,
    )
    return {"outcome": outcome}


@router.post(
    "/departments/{dept_id}/restore",
    response_model=DepartmentDTO,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def restore_department(
    dept_id: uuid.UUID, db: DbSession, _p: CurrentPrincipal
) -> DepartmentDTO:
    dept = await db.get(m.Department, dept_id)
    if dept is None or dept.deleted_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no archived department with that id")
    dept.deleted_at = None
    await db.flush()
    # `department_to_dto` is a pure in-memory serialization of the already-
    # loaded `dept` row -- no DB read of its own, so building the DTO here
    # (never after a `db.commit()`, which this route never issues) carries
    # none of the "tenant GUC dies at commit" risk.
    return department_to_dto(dept)


async def _visible_department_or_404(
    db: DbSession, *, actor: HumanActor, tenant_wide: bool, dept_id: uuid.UUID
) -> m.Department:
    dept = await visible_department(
        db, scope=actor.scope, tenant_wide=tenant_wide, department_id=dept_id
    )
    if dept is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
    return dept


@router.get(
    "/departments/{dept_id}",
    response_model=DepartmentDTO,
)
async def get_department(
    dept_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(DEPARTMENT, VIEW)))],
) -> DepartmentDTO:
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(DEPARTMENT, VIEW))
    dept = await _visible_department_or_404(
        db, actor=actor, tenant_wide=tenant_wide, dept_id=dept_id
    )
    return department_to_dto(dept)


@router.get(
    "/departments/{dept_id}/tools",
    response_model=DepartmentToolsDTO,
)
async def get_department_tools(
    dept_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(DEPARTMENT, VIEW)))],
) -> DepartmentToolsDTO:
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(DEPARTMENT, VIEW))
    dept = await _visible_department_or_404(
        db, actor=actor, tenant_wide=tenant_wide, dept_id=dept_id
    )
    tools = dict((dept.frame or {}).get("tools", {}))
    agents = (
        (
            await db.execute(
                select(m.Agent).where(
                    m.Agent.department_id == dept_id, m.Agent.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    return DepartmentToolsDTO(
        tools=tools,
        deviation_counts=_deviation_counts(tools, agents),
        agent_count=len(agents),
    )


@router.put(
    "/departments/{dept_id}/tools",
    response_model=DepartmentToolsDTO,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def set_department_tools(
    dept_id: uuid.UUID,
    body: DepartmentToolsWriteRequest,
    db: DbSession,
    _p: CurrentPrincipal,
) -> DepartmentToolsDTO:
    dept = await _get_department(db, dept_id)
    frame = dict(dept.frame or {})
    old_tools = dict(frame.get("tools", {}))
    new_tools = {k: v.model_dump() for k, v in body.tools.items()}

    value_spec_violations: list[dict[str, str]] = []
    for key, policy in body.tools.items():
        # `only` is a plain tool-name allowlist -- meaningful for ANY
        # connection that has tools to restrict, with no dependency on a
        # `value_spec` (github_mcp/jira_mcp/microsoft365/google_workspace all
        # ship real `only`-based guardrail presets and declare no value_spec
        # at all; the frontend's own drawer already gates `only`'s visibility
        # on the connection having tool names, not on `hasValueSpec` --
        # `tool-guardrail-editor-drawer.tsx`). Only `approval_eur` (a
        # monetary threshold) genuinely needs to know a value_spec exists,
        # since that's where the threshold gets compared against.
        #
        # `only` is a list, not a nullable scalar like `approval_eur` -- the
        # frontend's GuardrailValue always sends `only: []` for a tool that
        # never had an allowlist, never `null`, so an `is not None` check
        # (matching approval_eur's real absent/present distinction) treats
        # every save as "wants an allowlist". Only a genuinely non-empty list
        # means the operator actually wants one.
        wants_eur = policy.approval_eur is not None
        if not wants_eur:
            continue
        # `credential_id.is_(None)` picks the manifest row, never another
        # login sharing this same tenant-global name -- `POST /mcp/logins`
        # creates a SECOND `McpConnection` row with the same `name` (see its
        # own docstring), and a bare `.where(name == ...)` here would raise
        # `MultipleResultsFound` for any tenant that has pinned a login on
        # exactly the connections that have a value_spec.
        mcp_conn = (
            await db.execute(
                select(m.McpConnection)
                .where(
                    m.McpConnection.tenant_id == _p.tenant_id,
                    m.McpConnection.name == key,
                    m.McpConnection.credential_id.is_(None),
                )
                .order_by(m.McpConnection.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        _cfg = mcp_conn.config if mcp_conn is not None and isinstance(mcp_conn.config, dict) else {}
        manifest_conn = resolve_tool_pack_connection(
            str(_cfg.get("_plugin_name", "")), str(_cfg.get("_connection_key", ""))
        )
        if not connection_supports_value_spec(manifest_conn):
            value_spec_violations.append({"connection": key, "field": "approval_eur"})
    if value_spec_violations:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "value_spec_not_supported", "violations": value_spec_violations},
        )

    frame["tools"] = new_tools
    dept.frame = frame

    # Cascade each tool's new `enabled` default to every CURRENT agent in this
    # department -- but only the ones who have never *deliberately* touched
    # this tool key. Provenance is tracked explicitly, not inferred: see
    # `Agent.narrowing_overridden_keys` (models/core.py) and
    # `agents_write.py`'s `set_narrowing`, the ONLY place anywhere that ever
    # adds to that set -- it represents a real, deliberate operator action
    # (a human/API caller only calls `PUT /agents/{id}/narrowing` on purpose).
    # This endpoint is deliberately NOT a writer of `narrowing_overridden_keys`
    # -- only a reader -- which is the whole invariant that makes "has this
    # agent explicitly overridden this tool" unambiguous.
    #
    # An earlier version of this cascade tried to infer "has been overridden"
    # from `agent.narrowing["tools"][key]`'s mere presence, and later from a
    # value-comparison heuristic (does the stored value diverge from the OLD
    # department default) after the presence-only version proved to freeze a
    # never-touched agent after its first cascade write. Both were proven
    # unsound: this endpoint is itself a second writer of that same JSON
    # blob, so nothing inferred from the blob's own shape can reliably tell
    # apart a real operator choice from this cascade's own past residue --
    # a genuine override whose value happens to coincidentally equal the
    # department's OLD default at some later toggle would eventually get
    # silently swept by the value-comparison version, with no error and no
    # audit trail. `narrowing_overridden_keys` is a wholly separate, backend-
    # only column outside that blob, so it carries no such ambiguity: a key
    # is either in the set (an operator wrote it on purpose, at some point,
    # ever -- never touch it, no matter what value is currently stored or
    # what the department's history of toggles has done) or it isn't (safe to
    # write/overwrite unconditionally, whether the agent's own entry is
    # currently absent or holds residue from an earlier cascade run).
    #
    # This does not attempt to enforce Task 4's "enabled tool needs a
    # connection_id" login rule; the department toggle only ever sets a
    # default `enabled` state and never implies a login (agent tool login
    # selection design's own global constraint) -- an agent cascaded to
    # enabled without a connection_id is caught at runtime resolution, not
    # here, and re-saving that agent's own narrowing via `PUT .../narrowing`
    # still enforces Task 4's check as normal.
    agents = (
        (
            await db.execute(
                select(m.Agent).where(
                    m.Agent.department_id == dept_id, m.Agent.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    await _cascade_department_tools_change(
        db, old_tools=old_tools, new_tools=frame["tools"], agents=agents
    )

    await db.flush()
    return DepartmentToolsDTO(
        tools=frame["tools"],
        deviation_counts=_deviation_counts(frame["tools"], agents),
        agent_count=len(agents),
    )


@router.post(
    "/departments/{dept_id}/tools/{key}/reset",
    response_model=DepartmentToolsDTO,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def reset_department_tool(
    dept_id: uuid.UUID,
    key: str,
    db: DbSession,
    _p: CurrentPrincipal,
) -> DepartmentToolsDTO:
    """Restore one tool key to its CAPA default, discarding whatever hand-
    edit an operator made to it at the department level (Source flips back
    from "department" to "capa_default" -- see `authz.pdp.tool_policy_
    source`). Reads `Department.frame_capa_defaults`, the snapshot captured
    once at template-instantiation time and never touched again (its own
    docstring in models/core.py); a department with none at all -- hand-
    created with no template, or one that predates the column -- has nothing
    to restore TO, so the key is removed from the frame entirely rather than
    silently kept as this operator's own prior edit under a different name.

    Cascades exactly like `set_department_tools`, via the same shared
    `_cascade_department_tools_change` helper, so an agent that never
    deliberately overrode this key sees the restored value too.
    """
    dept = await _get_department(db, dept_id)
    frame = dict(dept.frame or {})
    old_tools = dict(frame.get("tools", {}))
    if key not in old_tools:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"tool {key!r} not set on this department")

    new_tools = dict(old_tools)
    capa_defaults = dict((dept.frame_capa_defaults or {}).get("tools", {}))
    if key in capa_defaults:
        new_tools[key] = dict(capa_defaults[key])
    else:
        del new_tools[key]
    frame["tools"] = new_tools
    dept.frame = frame

    agents = (
        (
            await db.execute(
                select(m.Agent).where(
                    m.Agent.department_id == dept_id, m.Agent.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    await _cascade_department_tools_change(
        db, old_tools=old_tools, new_tools=new_tools, agents=agents
    )

    await db.flush()
    await append_event(
        db,
        tenant_id=_p.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="authz",
        action="department.tools.reset_to_capa_default",
        resource={"department_id": str(dept_id), "key": key, "by": _p.subject},
        principal=_p,
    )
    return DepartmentToolsDTO(
        tools=new_tools,
        deviation_counts=_deviation_counts(new_tools, agents),
        agent_count=len(agents),
    )


@router.get(
    "/departments/{dept_id}/agents",
    response_model=list[AgentDTO],
)
async def department_agents(
    dept_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(DEPARTMENT, VIEW)))],
) -> list[AgentDTO]:
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(DEPARTMENT, VIEW))
    await _visible_department_or_404(db, actor=actor, tenant_wide=tenant_wide, dept_id=dept_id)
    # `visible_agents` returns `(rows, total_count)` as of Task 6 -- this route's
    # own response_model stays a bare `list[AgentDTO]` (not `Page`), so the
    # count is simply discarded here.
    rows, _total = await visible_agents(
        db, scope=actor.scope, tenant_wide=tenant_wide, department_id=dept_id
    )
    return [agent_to_dto(a) for a in rows]


@router.get(
    "/departments/{dept_id}/board",
    response_model=BoardDTO,
)
async def department_board(
    dept_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(DEPARTMENT, VIEW)))],
    limit: int = 25,
) -> BoardDTO:
    """The newest `limit` tasks PER COLUMN, plus the full count of each.

    Per column rather than overall, because the columns fill at wildly different
    rates: an agent that has run for a week has hundreds of `done` tasks and
    three in progress, and a flat limit would return the hundreds and hide the
    three -- the ones somebody opened the board to look at.

    Newest first, which reverses the old order. A board that grows forever is
    read from the recent end; the first task a department ever ran is the least
    interesting row on the page.
    """
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(DEPARTMENT, VIEW))
    await _visible_department_or_404(db, actor=actor, tenant_wide=tenant_wide, dept_id=dept_id)

    limit = max(1, min(limit, 200))
    rows = (
        (
            await db.execute(
                select(m.Task)
                .where(m.Task.department_id == dept_id)
                .order_by(m.Task.created_at.desc(), m.Task.id.desc())
            )
        )
        .scalars()
        .all()
    )

    totals: dict[str, int] = {}
    per_column: dict[str, list[TaskDTO]] = {}
    for task in rows:
        dto = task_to_dto(task)
        totals[dto.column] = totals.get(dto.column, 0) + 1
        bucket = per_column.setdefault(dto.column, [])
        if len(bucket) < limit:
            bucket.append(dto)
    return BoardDTO(tasks=[dto for bucket in per_column.values() for dto in bucket], totals=totals)
