"""Agent lifecycle and configuration writes. Every write enforces the PDP where
relevant, is gated on department-scoped agent-write authority (`require_agent_
write` / `authorize_agent_write`, `api/deps.py`), and emits an audit event.

**The ordering rule every route here obeys**: `authorize_agent_write` runs
immediately after the target department is known -- straight after `_load_agent`
(or, for `create_agent`, straight after the department existence check) -- and
strictly BEFORE any frame-derived computation (`narrowing_within_frame`,
`missing_skill_requirements`). Calling it late would let a wrong-department
toggle holder reconstruct that department's tool frame one crafted request's
422 at a time before the refusal ever fires; see `api/deps.py::
authorize_agent_write`'s docstring for the full reasoning. Do not reorder these
calls without re-reading it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.hire import create_hire_request, require_hire_approval
from oc8.agents.presentation import show_model
from oc8.agents.publish_hooks import PublishHookFailed
from oc8.agents.versioning import (
    NoChangesToPublish,
    apply_payload,
    draft_status,
    missing_references,
    pinned_model_config_id,
    publish_version,
    snapshot_agent,
    version_payload,
)
from oc8.api.deps import (
    DbSession,
    authorize_agent_write,
    require_agent_write,
    require_permission,
)
from oc8.api.v1._serializers import agent_to_dto, agent_version_to_dto
from oc8.api.v1.agents import _agent_detail_dto
from oc8.api.v1.files import _attachment_dto, _store_upload
from oc8.audit import append_event
from oc8.authz.authority import authority_for_principal
from oc8.authz.pdp import ToolPolicy, missing_skill_requirements, narrowing_within_frame
from oc8.authz.permissions import AGENT_VERSION_PUBLISH, KNOWLEDGE, MANAGE, perm
from oc8.authz.scope import HumanActor
from oc8.capas.discovery import connection_supports_value_spec, resolve_tool_pack_connection
from oc8.capas.manifest import GuardrailAttribute
from oc8.copilot.guardrail_interpret import (
    GuardrailNotUnderstood,
    interpret_guardrail_definition,
    interpret_guardrails_from_instruction,
)
from oc8.modelrouter.subscription_guard import (
    SubscriptionModelNotManualOnly,
    assert_manual_only_compatible,
)
from oc8.runtime.registry import (
    RuntimeCapabilityError,
    RuntimeNotExecutableError,
    RuntimeNotFoundError,
    assign_runtime,
    check_runtime_capabilities,
    resolve_runtime_plugin,
)
from oc8.schemas.dto import (
    AgentDetailDTO,
    AgentDTO,
    AgentVersionDTO,
    ConditionDTO,
    FileAttachmentDTO,
    FunctionGuardrailInterpretationDTO,
    GuardrailBatchInterpretationDTO,
    GuardrailInterpretationDTO,
)
from oc8.schemas.requests import (
    AgentRenameRequest,
    AssignSkillRequest,
    CreateAgentRequest,
    GuardrailInterpretFromInstructionRequest,
    GuardrailInterpretRequest,
    InstructionsRequest,
    LifecycleRequest,
    ModelConfigRequest,
    NarrowingRequest,
    PublishAgentVersionRequest,
    RuntimeAssignRequest,
)

router = APIRouter()


def _violation_body(violations: list[Any]) -> dict[str, Any]:
    return {
        "error": "runtime_capability_violation",
        "missing": [
            {"kind": v.kind, "missingCapability": v.missing_capability, "reason": v.reason}
            for v in violations
        ],
    }


_LIFECYCLE = {"start": "running", "pause": "paused", "stop": "stopped"}


async def _load_agent(db: DbSession, agent_id: uuid.UUID) -> m.Agent:
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return agent


async def enforce_narrowing_logins(
    db: DbSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    narrowing: dict[str, Any],
    frame: dict[str, Any],
) -> None:
    """Shared by `create_agent` and `set_narrowing`. A tool key can name a
    Credential-backed login (an `McpConnection` row with `credential_id` set
    -- Task 3's `POST /mcp/logins`, tenant-global, no manifest lookup
    involved). Enabling that key without saying which login to use would
    leave the runtime to guess; require the choice explicit instead -- no
    silent fallback (agent tool login selection design's global constraint).
    A key with no such row -- every existing, non-login tool -- is untouched.

    Also records provenance in `narrowing_overridden_keys`: a tool key is
    recorded only when its submitted value actually differs from what the
    DEPARTMENT FRAME currently grants for that key by default -- not from
    what the agent's own prior narrowing happened to hold, which would mark
    an agent's very first save as "overridden" even when the submitted value
    merely matches the frame's own default (there is no prior narrowing to
    compare against yet). Comparing against the frame is also what "has this
    agent deliberately diverged from department policy" (the "Abweichungen"
    deviation count this column exists for) actually means. Append-only
    union, never removed here -- once a save's value diverges from the frame
    default AT THE TIME of that save, the key stays marked even if a later
    frame change happens to bring the two back into alignment; that's what
    keeps `set_department_tools`'s cascade (departments.py) from re-syncing a
    key the operator has ever deliberately touched. This is the ONLY writer
    of `narrowing_overridden_keys` anywhere in the codebase; the cascade
    reads it but must never add to it -- that asymmetry is what makes "has
    this agent explicitly overridden this tool" unambiguous, instead of
    having to infer it from `narrowing["tools"][key]`'s mere presence/value,
    which the cascade also writes into and is provably not a reliable signal
    on its own (agent tool login selection design, Task 5 fix round 2).
    """
    raw_tools = narrowing.get("tools", {}) if isinstance(narrowing, dict) else {}
    if isinstance(raw_tools, dict):
        for key, raw in raw_tools.items():
            policy = ToolPolicy.from_json(raw if isinstance(raw, dict) else None)
            if not policy.enabled:
                continue
            # `.limit(1)`: this is only an existence check ("does at least
            # one login exist for this key"), not a resolution of which one
            # -- a department-scoped login (agent tool login selection
            # design) can share `name` with another login under a different
            # department_id, and a bare `.scalar_one_or_none()` here would
            # raise `MultipleResultsFound` once that happens.
            login_conn = (
                await db.execute(
                    select(m.McpConnection)
                    .where(
                        m.McpConnection.tenant_id == tenant_id,
                        m.McpConnection.name == key,
                        m.McpConnection.credential_id.is_not(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if login_conn is not None and not policy.connection_id:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"tool {key!r} needs a login: set connection_id before enabling it",
                )
    if isinstance(raw_tools, dict) and raw_tools:
        frame_tools = frame.get("tools", {}) if isinstance(frame, dict) else {}
        overridden = set(agent.narrowing_overridden_keys or [])
        for key, raw in raw_tools.items():
            # A raw dict compare never matches: the frame stores
            # `default_connection_id`/`only: None`, the frontend's narrowing
            # payload stores `connection_id`/`only: []` for the same "unset"
            # meaning -- so every save marked every submitted key overridden,
            # regardless of whether its value actually diverged from the
            # frame. `ToolPolicy.from_json` is the same normalization
            # `effective_tool_policies`/`narrowing_within_frame` already
            # apply to these two dicts; comparing through it is what makes
            # this an actual value comparison instead of a shape comparison.
            submitted = ToolPolicy.from_json(raw if isinstance(raw, dict) else None)
            current_frame = ToolPolicy.from_json(frame_tools.get(key))
            if submitted != current_frame:
                overridden.add(key)
        agent.narrowing_overridden_keys = sorted(overridden)


@router.post(
    "/agents",
    response_model=AgentDetailDTO,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent(
    body: CreateAgentRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDetailDTO:
    dept = await db.get(m.Department, body.department_id)
    if dept is None or dept.deleted_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown department")

    # The department is now known. Authorize BEFORE `narrowing_within_frame` --
    # its 422 carries `dept.frame`, one violation at a time, and a caller
    # refused only after that call ran could reconstruct a department she has
    # no write authority in from the shape of the refusal alone.
    await authorize_agent_write(
        request,
        db,
        actor,
        dept.id,
        not_found=HTTPException(status.HTTP_400_BAD_REQUEST, "unknown department"),
    )

    if body.narrowing:
        violations = narrowing_within_frame(dept.frame, body.narrowing)
        if violations:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "error": "narrowing_exceeds_frame",
                    "violations": [v.__dict__ for v in violations],
                },
            )

    principal = actor.principal
    gated = await require_hire_approval(db, tenant_id=principal.tenant_id)
    agent = m.Agent(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        department_id=body.department_id,
        name=body.name,
        role_title=body.role_title,
        mission=body.mission,
        model_config_id=body.model_config_id,
        narrowing={},
        is_team_lead=body.is_team_lead,
        status="pending_approval" if gated else "stopped",
        trust_level="first_party",
        definition={"oc8_agent": 1, "name": body.name, "mission": body.mission},
        presentation=body.presentation,
    )
    if body.narrowing:
        await enforce_narrowing_logins(
            db,
            tenant_id=principal.tenant_id,
            agent=agent,
            narrowing=body.narrowing,
            frame=dept.frame or {},
        )
        agent.narrowing = body.narrowing
    db.add(agent)
    db.add(m.MemoryStore(tenant_id=principal.tenant_id, tier="agent", owner_id=agent.id))
    await db.flush()

    # Only when the caller actually named a runtime. Calling the helper with None
    # here would clear a ref that was never set and audit it as `runtime.cleared`
    # -- a deliberate-looking operator action, on every single hire, that nobody
    # performed. `PUT .../runtime` with an explicit null still goes through the
    # helper, because there the clear IS the operator's request.
    if body.runtime_plugin_id is not None:
        try:
            await assign_runtime(
                db,
                tenant_id=principal.tenant_id,
                agent=agent,
                runtime_ref=body.runtime_plugin_id,
                principal=principal,
            )
        except RuntimeNotFoundError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "runtime not found or not enabled"
            ) from exc
        except RuntimeNotExecutableError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "runtime not executable") from exc
        except RuntimeCapabilityError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, _violation_body(exc.violations)
            ) from exc

    # Publish v1 in the same transaction as the create -- after runtime
    # assignment, so a caller-requested runtime is already reflected in the
    # snapshot, and before the audit event below (skills_write.py's
    # create_skill follows the same create-and-publish-atomically shape).
    await publish_version(db, agent, published_by=actor.member.id)

    if gated:
        await create_hire_request(db, agent=agent)
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.hire_requested" if gated else "agent.created",
        resource={"agent_id": str(agent.id), "name": agent.name, "by": principal.subject},
        principal=principal,
    )
    return await _agent_detail_dto(db, agent)


@router.post(
    "/agents/{agent_id}/lifecycle",
    response_model=AgentDTO,
)
async def lifecycle(
    agent_id: uuid.UUID,
    body: LifecycleRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDTO:
    if body.action not in _LIFECYCLE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown lifecycle action")
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    if agent.status == "pending_approval":
        raise HTTPException(status.HTTP_409_CONFLICT, "agent awaiting hire approval")
    agent.status = _LIFECYCLE[body.action]
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action=f"agent.{body.action}",
        resource={"agent_id": str(agent.id), "status": agent.status, "by": principal.subject},
        principal=principal,
    )
    return agent_to_dto(agent)


@router.put(
    "/agents/{agent_id}/narrowing",
    response_model=AgentDetailDTO,
)
async def set_narrowing(
    agent_id: uuid.UUID,
    body: NarrowingRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDetailDTO:
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    dept = await db.get(m.Department, agent.department_id)
    frame = dept.frame if dept else {}
    violations = narrowing_within_frame(frame, body.narrowing)
    if violations:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "narrowing_exceeds_frame", "violations": [v.__dict__ for v in violations]},
        )

    raw_tools = body.narrowing.get("tools", {}) if isinstance(body.narrowing, dict) else {}
    value_spec_violations: list[dict[str, str]] = []
    if isinstance(raw_tools, dict):
        for key, raw in raw_tools.items():
            if not isinstance(raw, dict):
                continue
            # `only` is a plain tool-name allowlist -- meaningful for ANY
            # connection that has tools to restrict, with no dependency on a
            # `value_spec` (github_mcp/jira_mcp/microsoft365/google_workspace
            # all ship real `only`-based guardrail presets and declare no
            # value_spec at all; the frontend's own drawer already gates
            # `only`'s visibility on the connection having tool names, not on
            # `hasValueSpec` -- `tool-guardrail-editor-drawer.tsx`). Only
            # `approval_eur` (a monetary threshold) genuinely needs to know a
            # value_spec exists, since that's where the threshold gets
            # compared against.
            wants_eur = raw.get("approval_eur") is not None
            if not wants_eur:
                continue
            # `credential_id.is_(None)` picks the manifest row, never another
            # login sharing this same tenant-global name -- `POST /mcp/logins`
            # creates a SECOND `McpConnection` row with the same `name` (see
            # its own docstring), and a bare `.where(name == ...)` here would
            # raise `MultipleResultsFound` for any tenant that has pinned a
            # login on exactly the connections that have a value_spec.
            mcp_conn = (
                await db.execute(
                    select(m.McpConnection)
                    .where(
                        m.McpConnection.tenant_id == principal.tenant_id,
                        m.McpConnection.name == key,
                        m.McpConnection.credential_id.is_(None),
                    )
                    .order_by(m.McpConnection.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            _cfg: dict[str, Any] = {}
            if mcp_conn is not None and isinstance(mcp_conn.config, dict):
                _cfg = mcp_conn.config
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

    await enforce_narrowing_logins(
        db,
        tenant_id=principal.tenant_id,
        agent=agent,
        narrowing=body.narrowing,
        frame=frame,
    )
    agent.narrowing = body.narrowing
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="authz",
        action="agent.narrowing.updated",
        resource={"agent_id": str(agent.id), "by": principal.subject},
        principal=principal,
    )
    return await _agent_detail_dto(db, agent)


@router.post(
    "/agents/{agent_id}/narrowing/{key}/reset",
    response_model=AgentDetailDTO,
)
async def reset_agent_narrowing(
    agent_id: uuid.UUID,
    key: str,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDetailDTO:
    """Discard this agent's own override of one tool key, going back to
    whatever the department's current frame grants for it (Source flips back
    from "agent" to "department"/"capa_default" -- see `authz.pdp.tool_
    policy_source`). Unlike the department-level reset
    (`departments.py::reset_department_tool`), there is no separate defaults
    snapshot to restore FROM here: an agent's only two layers are its own
    narrowing and the department frame it inherits from, so "reset" simply
    means "stop narrowing this key" -- the frame itself, whatever it
    currently is, becomes this key's effective value again, with no cascade
    needed since this write touches only this one agent.

    Removing the key from `narrowing_overridden_keys` is required alongside
    removing it from `narrowing["tools"]`, not implied by the latter: a key
    can be recorded as overridden even when the agent's narrowed value
    happens to equal the frame's (`enforce_narrowing_logins`'s own
    docstring), so leaving it in `narrowing_overridden_keys` after this
    reset would make `set_department_tools`'s cascade skip this agent on the
    very next department-level change to this key.
    """
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    raw_tools = dict((agent.narrowing or {}).get("tools", {}))
    if key not in raw_tools:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"tool {key!r} not set on this agent")

    new_tools = dict(raw_tools)
    del new_tools[key]
    narrowing = dict(agent.narrowing or {})
    narrowing["tools"] = new_tools
    agent.narrowing = narrowing
    overridden = set(agent.narrowing_overridden_keys or [])
    overridden.discard(key)
    agent.narrowing_overridden_keys = sorted(overridden)

    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="authz",
        action="agent.narrowing.reset_to_inherited",
        resource={"agent_id": str(agent.id), "key": key, "by": principal.subject},
        principal=principal,
    )
    return await _agent_detail_dto(db, agent)


async def _connection_guardrail_attributes(
    db: DbSession, *, tenant_id: uuid.UUID, connection_name: str
) -> list[GuardrailAttribute]:
    """This connection's declared `GuardrailAttribute`s (`capas/manifest.py`)
    -- the closed catalog `interpret_guardrail_definition` may build
    `with_limits` conditions from. Same connection resolution `set_narrowing`
    uses for `approval_eur`, generalized: a plugin with no matching manifest
    (or one that declares none) yields an empty list, which is exactly what
    forces the interpreter away from `with_limits`."""
    mcp_conn = (
        await db.execute(
            select(m.McpConnection)
            .where(
                m.McpConnection.tenant_id == tenant_id,
                m.McpConnection.name == connection_name,
                m.McpConnection.credential_id.is_(None),
            )
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    cfg: dict[str, Any] = {}
    if mcp_conn is not None and isinstance(mcp_conn.config, dict):
        cfg = mcp_conn.config
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    return manifest_conn.guardrail_attributes if manifest_conn is not None else []


async def _connection_tool_catalog(
    db: DbSession, *, tenant_id: uuid.UUID, connection_name: str
) -> list[str]:
    """This connection's real tool names, resolved server-side the same way
    `GET /mcp/connections/{name}/tool-names` does -- the batch interpreter
    below must never trust a client-supplied function list, since that list
    also becomes the closed enum the LLM tool call is validated against."""
    mcp_conn = (
        await db.execute(
            select(m.McpConnection)
            .where(
                m.McpConnection.tenant_id == tenant_id,
                m.McpConnection.name == connection_name,
                m.McpConnection.credential_id.is_(None),
            )
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    cfg: dict[str, Any] = {}
    if mcp_conn is not None and isinstance(mcp_conn.config, dict):
        cfg = mcp_conn.config
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if manifest_conn is None:
        return []
    scopes = manifest_conn.scopes if isinstance(manifest_conn.scopes, dict) else {}
    return sorted({*scopes.get("read", []), *scopes.get("modify", [])})


@router.post(
    "/agents/{agent_id}/guardrails/interpret-from-instruction",
    response_model=GuardrailBatchInterpretationDTO,
)
async def interpret_guardrails_from_instruction_endpoint(
    agent_id: uuid.UUID,
    body: GuardrailInterpretFromInstructionRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> GuardrailBatchInterpretationDTO:
    """Same one-shot, non-conversational contract as `interpret_guardrail`
    below, but reads the agent's own instructions instead of an
    operator-typed definition, and proposes rules for every function of one
    connection in a single call ("Copilot" button in the guardrails table).
    Nothing is written here; the operator still reviews and must explicitly
    accept each suggestion, same as the free-text interpreter."""
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    tool_catalog = await _connection_tool_catalog(
        db, tenant_id=principal.tenant_id, connection_name=body.connection_name
    )
    attributes = await _connection_guardrail_attributes(
        db, tenant_id=principal.tenant_id, connection_name=body.connection_name
    )
    try:
        results = await interpret_guardrails_from_instruction(
            db,
            tenant_id=principal.tenant_id,
            agent=agent,
            connection_name=body.connection_name,
            tool_catalog=tool_catalog,
            guardrail_attributes=attributes,
        )
    except GuardrailNotUnderstood as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, {"error": "guardrail_not_understood"}
        ) from exc
    return GuardrailBatchInterpretationDTO(
        results=[
            FunctionGuardrailInterpretationDTO(
                function=r.function,
                decision=r.decision,
                conditions=[
                    ConditionDTO(
                        attribute=c.attribute,
                        datatype=c.datatype,
                        operator=c.operator,
                        value=list(c.value) if isinstance(c.value, tuple) else c.value,
                        then=c.then.value,
                    )
                    for c in r.conditions
                ],
            )
            for r in results
        ]
    )


@router.post(
    "/agents/{agent_id}/guardrails/interpret",
    response_model=GuardrailInterpretationDTO,
)
async def interpret_guardrail(
    agent_id: uuid.UUID,
    body: GuardrailInterpretRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> GuardrailInterpretationDTO:
    """Translate a free-text guardrail definition into the generic 4-state
    decision (plus structured `Condition`s for `with_limits`) -- a pre-fill
    suggestion only, never applied or re-interpreted at runtime. Nothing is
    written here; the operator still reviews, must explicitly accept it in
    the UI, and saves through `PUT /agents/{id}/narrowing` like any other
    narrowing change."""
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    attributes = await _connection_guardrail_attributes(
        db, tenant_id=principal.tenant_id, connection_name=body.connection_name
    )
    try:
        interpretation = await interpret_guardrail_definition(
            db,
            tenant_id=principal.tenant_id,
            agent=agent,
            connection_name=body.connection_name,
            function=body.function,
            definition=body.definition,
            guardrail_attributes=attributes,
        )
    except GuardrailNotUnderstood as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, {"error": "guardrail_not_understood"}
        ) from exc
    return GuardrailInterpretationDTO(
        decision=interpretation.decision,
        conditions=[
            ConditionDTO(
                attribute=c.attribute,
                datatype=c.datatype,
                operator=c.operator,
                value=list(c.value) if isinstance(c.value, tuple) else c.value,
                then=c.then.value,
            )
            for c in interpretation.conditions
        ],
    )


@router.put(
    "/agents/{agent_id}/runtime",
    response_model=AgentDetailDTO,
)
async def assign_agent_runtime(
    agent_id: uuid.UUID,
    body: RuntimeAssignRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDetailDTO:
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal

    try:
        await assign_runtime(
            db,
            tenant_id=principal.tenant_id,
            agent=agent,
            runtime_ref=body.runtime_plugin_id,
            principal=principal,
        )
    except RuntimeNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "runtime not found or not enabled") from exc
    except RuntimeNotExecutableError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "runtime not executable") from exc
    except RuntimeCapabilityError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, _violation_body(exc.violations)
        ) from exc

    return await _agent_detail_dto(db, agent)


@router.patch(
    "/agents/{agent_id}/model-config",
    response_model=AgentDTO,
)
async def switch_model(
    agent_id: uuid.UUID,
    body: ModelConfigRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDTO:
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    mc = await db.get(m.ModelConfig, body.model_config_id)
    if mc is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown model config")
    # Unlike triggers.py's create/update guard calls (which must run AFTER
    # their own flush, because they check a Trigger row the request itself
    # just created), this check can safely run BEFORE the mutation below:
    # both `agent.id` and `mc.id` already name pre-existing, already-flushed
    # rows -- `mc` was just loaded with `db.get`, and this route never
    # creates a Trigger -- so the guard's own queries (the model's fallback
    # chain, the agent's already-enabled triggers) see identical state
    # whichever side of `agent.model_config_id = mc.id` they run on. That
    # assignment is an in-memory attribute the guard never reads. Verified
    # empirically: swapping the two lines produced the same four pass/fail
    # results (Task 11 report).
    try:
        await assert_manual_only_compatible(db, agent_id=agent.id, model_config_id=mc.id)
    except SubscriptionModelNotManualOnly as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    agent.model_config_id = mc.id
    show_model(agent, mc)
    # jsonb: replaced whole, or SQLAlchemy never notices the mutation. Each
    # of the four sampling overrides is independent -- a save that doesn't
    # mention a field (not in model_fields_set) leaves whatever this agent
    # already had for it untouched, matching catalog.py's update_model's own
    # per-field semantics; a field present but null clears it back to
    # "inherit the assigned ModelConfig's own value" (resolve_params).
    definition = dict(agent.definition or {})
    model_params = dict(definition.get("model_params") or {})
    if "temperature" in body.model_fields_set:
        if body.temperature is not None:
            model_params["temperature"] = body.temperature
        else:
            model_params.pop("temperature", None)
    if "max_tokens" in body.model_fields_set:
        if body.max_tokens is not None:
            model_params["max_tokens"] = body.max_tokens
        else:
            model_params.pop("max_tokens", None)
    if "effort" in body.model_fields_set:
        if body.effort and body.effort.strip():
            model_params["effort"] = body.effort.strip()
        else:
            model_params.pop("effort", None)
    if "extra" in body.model_fields_set:
        if body.extra:
            model_params["extra"] = body.extra
        else:
            model_params.pop("extra", None)
    if model_params:
        definition["model_params"] = model_params
    else:
        definition.pop("model_params", None)
    # Top-level key, not nested under model_params: engine._max_steps reads
    # agent.definition["max_steps"] directly.
    if "max_steps" in body.model_fields_set:
        if body.max_steps is not None and body.max_steps > 0:
            definition["max_steps"] = body.max_steps
        else:
            definition.pop("max_steps", None)
    agent.definition = definition
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.model.switched",
        resource={"agent_id": str(agent.id), "model": mc.model, "by": principal.subject},
        principal=principal,
    )
    return agent_to_dto(agent)


@router.patch(
    "/agents/{agent_id}/instructions",
    response_model=AgentDetailDTO,
)
async def update_instructions(
    agent_id: uuid.UUID,
    body: InstructionsRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDetailDTO:
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    before = agent.mission
    agent.mission = body.instructions
    await db.flush()
    # No-op edits (before == after, e.g. Save clicked without changing
    # anything) still get a revision row -- paperclip's own bundle history
    # does the same, and a caller relying on "one Save = one entry" would
    # otherwise have to special-case it.
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.instructions.updated",
        resource={
            "agent_id": str(agent.id),
            "before": before,
            "after": agent.mission,
            "by": principal.subject,
        },
        principal=principal,
    )
    return await _agent_detail_dto(db, agent)


@router.patch(
    "/agents/{agent_id}/name",
    response_model=AgentDetailDTO,
)
async def rename_agent(
    agent_id: uuid.UUID,
    body: AgentRenameRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDetailDTO:
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    before = agent.name
    agent.name = body.name
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.renamed",
        resource={"agent_id": str(agent.id), "before": before, "after": agent.name},
        principal=principal,
    )
    return await _agent_detail_dto(db, agent)


@router.post(
    "/agents/{agent_id}/instruction-files",
    response_model=FileAttachmentDTO,
    status_code=status.HTTP_201_CREATED,
)
async def upload_instruction_file(
    agent_id: uuid.UUID,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
    file: UploadFile,
) -> FileAttachmentDTO:
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    row = await _store_upload(
        db,
        tenant_id=actor.principal.tenant_id,
        owner_type="agent_instructions",
        owner_id=agent.id,
        file=file,
    )
    await db.commit()
    return _attachment_dto(row)


# `list_instruction_files` (GET /agents/{agent_id}/instruction-files) lives in
# `api/v1/agents.py`, not here: it's a read, and this file's own door
# (`require_agent_write`) is for `agent:manage`-parity mutations only. See
# that module's docstring, and `_owned_attachment` in `files.py`, which
# applies the identical view-level check to the SAME owner type
# ("agent_instructions") for GET/DELETE /files/{id} -- listing must not be
# more restrictive than downloading or deleting an individual file by id.


@router.delete(
    "/agents/{agent_id}/memory/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_agent_memory(
    agent_id: uuid.UUID,
    record_id: uuid.UUID,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> Response:
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    # Scoped by store_id, not just tenant_id -- a record id that exists but
    # belongs to a DIFFERENT agent's (or the department's) store must 404
    # here the same way a foreign agent_id does, not silently delete across
    # owners just because RLS already narrowed the query to this tenant.
    store = (
        await db.execute(
            select(m.MemoryStore).where(
                m.MemoryStore.tenant_id == principal.tenant_id,
                m.MemoryStore.tier == "agent",
                m.MemoryStore.owner_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    record = (
        None
        if store is None
        else (
            await db.execute(
                select(m.MemoryRecord).where(
                    m.MemoryRecord.id == record_id, m.MemoryRecord.store_id == store.id
                )
            )
        ).scalar_one_or_none()
    )
    if (
        record is not None
        and agent.is_tenant_assistant
        and "member_id" in (record.record_metadata or {})
    ):
        # A member's personal Copilot note (§7a.5): only that member deletes it,
        # through /copilot/notes. An admin here gets the same 404 as a miss.
        record = None
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory record not found")
    resource = {"agent_id": str(agent_id), "record_id": str(record_id)}
    if not agent.is_tenant_assistant:
        # Never a Copilot note's content in the audit trail: it is personal.
        resource["content_snippet"] = record.content[:200]
    resource["by"] = principal.subject
    await db.delete(record)
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.memory.deleted",
        resource=resource,
        principal=principal,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/agents/{agent_id}/skills",
    status_code=status.HTTP_201_CREATED,
)
async def assign_skill(
    agent_id: uuid.UUID,
    body: AssignSkillRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> dict[str, str]:
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    version = await db.get(m.SkillVersion, body.skill_version_id)
    if version is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown skill version")
    dept = await db.get(m.Department, agent.department_id)
    frame = dept.frame if dept else {}
    requires = version.definition.get("requires", {})

    granted = await db.execute(
        select(m.KnowledgeGrant.kb_id).where(
            m.KnowledgeGrant.grantee_id.in_([agent.id, agent.department_id])
        )
    )
    granted_kb_ids = {str(k) for k in granted.scalars().all()}

    missing = missing_skill_requirements(frame, agent.narrowing, requires, granted_kb_ids)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "requires_exceed_effective", "missing": [x.__dict__ for x in missing]},
        )

    resolved = await resolve_runtime_plugin(db, tenant_id=principal.tenant_id, agent=agent)
    if resolved is not None:
        _, runtime_version = resolved
        violations = check_runtime_capabilities(
            has_supervision=False,
            has_enabled_skills=True,
            runtime_capabilities=list(runtime_version.capabilities),
        )
        if violations:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _violation_body(violations))

    # Already assigned is not an error worth a 500. The unique index reported the
    # duplicate faithfully and the exception went straight out as an Internal
    # Server Error, which tells an operator that oc8 broke rather than that
    # nothing needed doing.
    existing = (
        await db.execute(
            select(m.SkillAssignment).where(
                m.SkillAssignment.tenant_id == principal.tenant_id,
                m.SkillAssignment.agent_id == agent.id,
                m.SkillAssignment.skill_version_id == version.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not existing.enabled:
            existing.enabled = True
            # `flush`, not `commit`: a commit inside `tenant_session` unbinds
            # `app.tenant_id` for every statement issued afterward on this same
            # session, and RLS does not raise for that -- it silently returns
            # zero rows. `get_db` commits once, when the request finishes, same
            # as every other write in this file -- the route itself never does.
            await db.flush()
        # Same shape as the fresh path, different status: a caller that says
        # "assigned" when nothing changed teaches the operator to distrust it.
        return {"status": "already_assigned"}

    db.add(
        m.SkillAssignment(
            tenant_id=principal.tenant_id,
            agent_id=agent.id,
            skill_version_id=version.id,
            enabled=True,
        )
    )
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="skill.assigned",
        resource={"agent_id": str(agent.id), "skill_version_id": str(version.id)},
        principal=principal,
    )
    return {"status": "assigned"}


async def _publish_or_refuse(
    db: DbSession,
    agent: m.Agent,
    *,
    note: str | None,
    published_by: uuid.UUID,
    current_no: int | None,
    meta: dict[str, Any] | None = None,
) -> m.AgentVersion:
    """`publish_version` with its two refusals translated to HTTP. Shared by
    publish and rollback so the two answer identically -- a rollback IS a
    publish, and a client must not need two error vocabularies for one act.

    A publish hook that refuses comes back 422, not 409: a 409 says "your view
    of the world is out of date", and a compliance gate refusing is not that --
    the request was well-formed and the state was current, and the answer is
    still no. `get_db` rolls back on any exception leaving the route, so the
    version row added inside `publish_version` (and, for a rollback, every
    working-copy write before it) never reaches the database -- which is what
    makes a hook a veto rather than a complaint after the fact.
    """
    try:
        return await publish_version(db, agent, note=note, published_by=published_by, meta=meta)
    except NoChangesToPublish as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": "no_changes_to_publish", "currentVersionNo": current_no},
        ) from exc
    except PublishHookFailed as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "error": "publish_hook_rejected",
                "hook": exc.hook_name,
                "reason": str(exc.cause),
            },
        ) from exc


async def _current_version_no(db: DbSession, agent: m.Agent) -> int | None:
    if agent.current_version_id is None:
        return None
    current = await db.get(m.AgentVersion, agent.current_version_id)
    return current.version_no if current is not None else None


@router.post(
    "/agents/{agent_id}/versions",
    response_model=AgentVersionDTO,
    status_code=status.HTTP_201_CREATED,
    # TWO gates, and both are needed. This one is the tenant-wide
    # `agent_version:publish` -- the governed act (spec §6): an editor may change
    # a draft with `agent:manage` and still not be able to put it into
    # production. `require_agent_write` below is the department door, and
    # `authorize_agent_write` in the body is the per-agent narrow.
    dependencies=[Depends(require_permission(AGENT_VERSION_PUBLISH))],
)
async def publish_agent_version(
    agent_id: uuid.UUID,
    body: PublishAgentVersionRequest,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentVersionDTO:
    """Turn the working copy into the numbered version that runs (spec §2.7).

    Two 409s, checked in this order:

    * **stale** -- `expected_current_version_no` disagrees with what is current,
      so somebody else published while this operator was editing. The client
      must refetch.
    * **no-op** -- the snapshot hashes identically to the current version.
      There is nothing to publish and retrying will never help.

    Staleness first, because the two carry opposite instructions: a stale client
    whose draft also happens to be clean must be told to refetch, and "nothing
    to publish" would send it away still holding a version number that moved.

    `publish_version` appends `agent.version.published` itself, so this route
    appends nothing extra -- one operator action, one event.
    """
    agent = await _load_agent(db, agent_id)
    # The department is now known. Authorize BEFORE the snapshot below, which
    # reads `agent.narrowing` -- exactly the frame-derived shape a wrong-
    # department caller must not learn from a response. Same ordering rule as
    # every other route in this module; see the module docstring.
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )

    current_no = await _current_version_no(db, agent)
    if body.expected_current_version_no != current_no:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "error": "stale_version",
                "expected": body.expected_current_version_no,
                "current": current_no,
            },
        )

    version = await _publish_or_refuse(
        db,
        agent,
        note=body.note,
        # `member.id`, not `principal.subject`: `published_by` is a UUID column
        # and the subject is an identity-provider string. Every other
        # attribution column in the schema names the member row.
        published_by=actor.member.id,
        current_no=current_no,
    )
    await db.flush()
    return agent_version_to_dto(version, current_version_id=agent.current_version_id)


@router.post(
    "/agents/{agent_id}/versions/{version_no}/rollback",
    response_model=AgentVersionDTO,
    status_code=status.HTTP_201_CREATED,
    # `agent_version:publish`, not a permission of its own: a rollback IS a
    # publish (spec §2.7, §6). Two names for one authority would mean the first
    # tenant to grant one without the other discovers they are the same thing.
    dependencies=[Depends(require_permission(AGENT_VERSION_PUBLISH))],
)
async def rollback_agent_version(
    agent_id: uuid.UUID,
    version_no: int,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentVersionDTO:
    """Copy an old version onto the working copy and publish it as a NEW one.

    Deliberately not a repoint of `current_version_id`, even though the Skill
    precedent repoints (`skills_write.py::create_skill_version`). Three reasons,
    from spec §2.7 and decision 3: version numbers stay monotonic in TIME, so
    "what was live on date X" is answerable by ordering alone; the rollback
    re-enters `publish_version` and therefore re-runs the publish hooks, which a
    repoint would silently skip; and it is `git revert`, not `git reset`.

    No request body; the note is generated (`"Rollback to vN"`).

    **The working copy is overwritten**, unpublished edits included (spec §2.7:
    the payload is copied "onto the agent row"; the UI confirms with the diff
    first). Which fields were discarded is recorded on the rollback's audit
    event, so a draft is never lost without trace. Every refusal below --
    including the no-op 409 and a refusing hook, both raised AFTER the
    overwrite -- leaves the route by exception, and `get_db` rolls the whole
    transaction back, so a refused rollback never touches the draft.

    Refusals, all BEFORE anything is written:

    * **403 `knowledge:manage`** -- only when the rollback would add or remove
      one of the agent's knowledge grants. That is a grant write, and the grant
      route itself requires `knowledge:manage`; `agent_version:publish` alone
      (a department manager) must not reach it by the back door. A rollback
      that leaves the grant set unchanged needs nothing extra.
    * **422 `narrowing_exceeds_frame`** -- the restored narrowing no longer fits
      the department frame as it stands TODAY. The PDP would intersect at
      runtime anyway, but every other writer of `agent.narrowing` validates
      first, and this must not become the one door that stores an out-of-frame
      value.
    * **422 `version_references_missing`** -- the target names a knowledge
      base, skill version or model config that has since been deleted.
      Restoring it would re-create the dangling grant `delete_base` removes as
      an authz hazard; dropping it would publish a version that is not the one
      asked for. See `versioning.missing_references`.
    * **422 subscription model** -- the restored `model_config_id` is one
      `switch_model` would refuse for this agent (a ChatGPT subscription with
      an enabled trigger). Same one-door reasoning as the frame check.

    Deliberately NOT re-run, because they guard an operator's FRESH input and a
    rolled-back value already passed them when first written: `set_narrowing`'s
    `value_spec` check and `enforce_narrowing_logins`. The latter also MUTATES
    `narrowing_overridden_keys`, a versioned field this rollback is restoring,
    so running it would corrupt the provenance being recovered. Residual risk:
    a login or runtime the restored version names that has since been removed
    fails at run time, not here.
    """
    agent = await _load_agent(db, agent_id)
    # Before the target is even loaded, and long before `dept.frame` is read:
    # the 422 below carries violations derived from that frame.
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal

    target = (
        await db.execute(
            select(m.AgentVersion).where(
                m.AgentVersion.agent_id == agent.id,
                m.AgentVersion.version_no == version_no,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")

    payload = version_payload(target)

    # A rollback that changes the agent's knowledge grants IS a grant write,
    # and `POST /knowledge/grants` requires `knowledge:manage` -- which
    # `dept_manager` holds `agent_version:publish` without. Unchecked, rollback
    # would be the side door that re-creates a grant an admin revoked (a
    # confidential base pulled after an incident) or drops one added since.
    # Only when the grant set actually CHANGES: rolling back a mission must not
    # suddenly need knowledge authority.
    live_grants = set((await snapshot_agent(db, agent))["knowledge_grants"])
    wanted_grants = {str(k) for k in payload.get("knowledge_grants") or []}
    grants_added = sorted(wanted_grants - live_grants)
    grants_removed = sorted(live_grants - wanted_grants)
    if "knowledge_grants" not in payload:
        # `apply_payload` leaves an absent key alone, so nothing would change.
        grants_added, grants_removed = [], []
    if grants_added or grants_removed:
        authority = await authority_for_principal(request, db, principal)
        if perm(KNOWLEDGE, MANAGE) not in authority.tenant_wide:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires permission: {perm(KNOWLEDGE, MANAGE)} "
                "(this rollback changes the agent's knowledge grants)",
            )
    dept = await db.get(m.Department, agent.department_id)
    frame = dept.frame if dept else {}
    violations = narrowing_within_frame(frame, payload.get("narrowing") or {})
    if violations:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "narrowing_exceeds_frame", "violations": [v.__dict__ for v in violations]},
        )

    missing = await missing_references(db, payload)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "version_references_missing", "missing": missing},
        )

    restored_model = pinned_model_config_id(payload)
    if restored_model != agent.model_config_id:
        try:
            await assert_manual_only_compatible(
                db, agent_id=agent.id, model_config_id=restored_model
            )
        except SubscriptionModelNotManualOnly as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    current_no = await _current_version_no(db, agent)
    # Read BEFORE the overwrite: this is the draft about to be discarded.
    discarded = (await draft_status(db, agent)).changed_fields

    model_before = agent.model_config_id
    await apply_payload(db, agent, payload)
    if agent.model_config_id != model_before:
        # `presentation.llm/provider` is not versioned, so nothing restored it;
        # without this the agent list keeps naming the pre-rollback model.
        show_model(
            agent,
            await db.get(m.ModelConfig, agent.model_config_id)
            if agent.model_config_id is not None
            else None,
        )
    await db.flush()
    version = await _publish_or_refuse(
        db,
        agent,
        note=f"Rollback to v{version_no}",
        published_by=actor.member.id,
        current_no=current_no,
        # The reserved `_meta` key, which `payload_hash` excludes -- see
        # META_KEY in agents/versioning.py. Inside the hash it would break the
        # no-op 409 for every agent that has ever been rolled back.
        meta={"rolled_back_from": version_no},
    )

    # A SECOND event, on top of `publish_version`'s own
    # `agent.version.published`. The first records that a version exists; this
    # records the operator action, its target, and what it threw away.
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.version.rolled_back",
        resource={
            "agent_id": str(agent.id),
            "version_no": version.version_no,
            "rolled_back_from": version_no,
            "discarded_draft_fields": list(discarded),
            "knowledge_grants_added": grants_added,
            "knowledge_grants_removed": grants_removed,
            "by": principal.subject,
        },
        principal=principal,
    )
    await db.flush()
    return agent_version_to_dto(version, current_version_id=agent.current_version_id)


@router.delete(
    "/agents/{agent_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_agent(
    agent_id: uuid.UUID,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> dict[str, str]:
    """Hard-delete when nothing depends on the agent; archive (soft-delete)
    otherwise -- a run's `agent_id` must keep pointing at a real row (same
    dependents-detection shape as `skills_write.py::delete_skill`)."""
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "agent not found"),
    )
    principal = actor.principal
    dependents = (
        await db.execute(
            select(func.count()).select_from(m.AgentRun).where(m.AgentRun.agent_id == agent_id)
        )
    ).scalar_one()
    now = dt.datetime.now(tz=dt.UTC)
    if dependents == 0:
        await db.delete(agent)
        outcome = "deleted"
    else:
        agent.deleted_at = now
        outcome = "archived"
    # `flush`, not `commit`: same reasoning as `assign_skill` above -- the
    # route body never commits itself, `get_db`/`tenant_session` commits once
    # when the request finishes, and a commit here would unbind `app.tenant_id`
    # for nothing this route still needs to read.
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action=f"agent.{outcome}",
        resource={"agent_id": str(agent_id), "by": principal.subject},
        principal=principal,
    )
    return {"outcome": outcome}


@router.post(
    "/agents/{agent_id}/restore",
    response_model=AgentDTO,
)
async def restore_agent(
    agent_id: uuid.UUID,
    db: DbSession,
    request: Request,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> AgentDTO:
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.deleted_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no archived agent with that id")
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, "no archived agent with that id"),
    )
    # Restoring is the moment an agent's triggers start firing again, so it is
    # the moment the manual-trigger-only rule starts applying to it again.
    # `subscription_guard` counts archived agents on both of its directions
    # (see `assert_credential_bind_safe`), so no route should be able to leave
    # an archived agent in this state -- this is the backstop for a pairing
    # that reached the database some other way (seed, plugin, direct SQL, a
    # future write path), checked BEFORE `deleted_at` is cleared so a refusal
    # leaves the agent archived rather than live-and-unattended. `agent` is
    # already loaded, so the common case costs one chain lookup that returns
    # before any trigger row is read.
    try:
        await assert_manual_only_compatible(
            db, agent_id=agent.id, model_config_id=agent.model_config_id
        )
    except SubscriptionModelNotManualOnly as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    principal = actor.principal
    agent.deleted_at = None
    await db.flush()
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.restored",
        resource={"agent_id": str(agent.id), "by": principal.subject},
        principal=principal,
    )
    # `agent_to_dto` is a pure in-memory serialization of the already-loaded
    # `agent` row (presentation/status/etc.) -- no DB read of its own, so
    # building the DTO here (never after a `db.commit()`, which this route
    # never issues) carries none of the "tenant GUC dies at commit" risk
    # `restore_skill` had to work around.
    return agent_to_dto(agent)
