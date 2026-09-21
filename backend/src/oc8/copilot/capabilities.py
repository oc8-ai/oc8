"""Closed capability registry for secret-blind Copilot operations.

No capability accepts arbitrary dictionaries.  In particular this module never
imports the secret store or an HTTP/shell client: a proposal may only reference
existing resources and invoke their established server-side service.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.hire import require_hire_approval
from oc8.authz import pdp
from oc8.automation.catalogue import list_installed_automation_events
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.manifest import GuardrailAttribute
from oc8.copilot.guardrail_interpret import (
    GuardrailNotUnderstood,
    attributes_for_function,
    parse_conditions,
)
from oc8.modelrouter.subscription_guard import (
    SubscriptionModelNotManualOnly,
    assert_manual_only_compatible,
)
from oc8.triggers.service import create_trigger


class _Operation(BaseModel):
    # JSON UUID references arrive as strings. Strict primitive validation still
    # comes from each field's declared type and the closed extra-key policy.
    model_config = ConfigDict(extra="forbid")


_LIFECYCLE = {"start": "running", "pause": "paused", "stop": "stopped"}


class MissionSet(_Operation):
    type: Literal["agent.mission.set"]
    agentId: uuid.UUID
    mission: str = Field(min_length=1, max_length=10_000)


class TriggerCreate(_Operation):
    type: Literal["trigger.create"]
    agentId: uuid.UUID
    kind: Literal["cron", "event"]
    taskText: str = Field(min_length=1, max_length=10_000)
    cronExpression: str | None = Field(default=None, max_length=256)
    eventSource: str | None = Field(default=None, max_length=128)
    eventType: str | None = Field(default=None, max_length=128)


class PluginEnable(_Operation):
    type: Literal["plugin.enable"]
    pluginId: uuid.UUID
    grantedPermissions: list[str] = Field(default_factory=list, max_length=64)


class IntegrationPrepare(_Operation):
    type: Literal["integration.prepare"]
    integrationId: uuid.UUID
    # A reference is intentionally all this operation can carry. Credential
    # material belongs to the integration's normal setup flow, outside Copilot.
    configurationRef: uuid.UUID | None = None


class DepartmentCreate(_Operation):
    type: Literal["department.create"]
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=2_000)
    icon: str = Field(default="building", max_length=100)


class DepartmentUpdate(_Operation):
    type: Literal["department.update"]
    departmentId: uuid.UUID
    name: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=2_000)
    icon: str | None = Field(default=None, max_length=100)


class DepartmentDelete(_Operation):
    type: Literal["department.delete"]
    departmentId: uuid.UUID


class DepartmentRestore(_Operation):
    type: Literal["department.restore"]
    departmentId: uuid.UUID


class AgentCreate(_Operation):
    type: Literal["agent.create"]
    departmentId: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    roleTitle: str = Field(default="", max_length=200)
    mission: str = Field(default="", max_length=10_000)


class AgentRename(_Operation):
    type: Literal["agent.rename"]
    agentId: uuid.UUID
    name: str = Field(min_length=1, max_length=200)


class AgentLifecycleSet(_Operation):
    type: Literal["agent.lifecycle.set"]
    agentId: uuid.UUID
    action: Literal["start", "pause", "stop"]


class AgentDelete(_Operation):
    type: Literal["agent.delete"]
    agentId: uuid.UUID


class AgentRestore(_Operation):
    type: Literal["agent.restore"]
    agentId: uuid.UUID


class GuardrailConditionInput(BaseModel):
    """One `payload.conditions[]` entry for `GuardrailSet` -- field-for-field
    the same shape as `authz.pdp.Condition`/`ConditionDTO`, kept a distinct
    model (rather than importing the API-layer `ConditionWriteDTO`) because
    this module never imports from `api/v1/*`. Validated for real by
    `guardrail_interpret.parse_conditions` at apply time, against the
    connection's actual declared `GuardrailAttribute`s -- this model only
    checks shape, not whether `attribute` exists or `value` matches its
    datatype.
    """

    model_config = ConfigDict(extra="forbid")
    attribute: str = Field(min_length=1, max_length=200)
    datatype: str
    operator: str
    value: Any = None
    then: str


class GuardrailSet(_Operation):
    """Same 4-state decision the manual editor and the free-text interpreter
    both write (`self_sufficient`/`with_limits`/`approval_required`/
    `not_allowed`) -- one policy model, three authoring paths. `conditions`
    is only meaningful (and required non-empty) when `decision ==
    "with_limits"`; it is the generic `Condition` list, never a euro-only
    field."""

    type: Literal["agent.guardrail.set"]
    agentId: uuid.UUID
    connectionName: str = Field(min_length=1, max_length=200)
    function: str = Field(min_length=1, max_length=200)
    decision: Literal["self_sufficient", "with_limits", "approval_required", "not_allowed"]
    conditions: list[GuardrailConditionInput] = Field(default_factory=list, max_length=20)


Operation = (
    MissionSet
    | TriggerCreate
    | PluginEnable
    | IntegrationPrepare
    | DepartmentCreate
    | DepartmentUpdate
    | DepartmentDelete
    | DepartmentRestore
    | AgentCreate
    | AgentRename
    | AgentLifecycleSet
    | AgentDelete
    | AgentRestore
    | GuardrailSet
)
_OPERATIONS = TypeAdapter(list[Operation])


class InvalidOperation(ValueError):
    """Safe, value-free validation failure exposed at every boundary."""

    def __init__(self) -> None:
        super().__init__("invalid copilot operation")


def parse_operations(raw: object) -> list[Operation]:
    try:
        operations = _OPERATIONS.validate_python(raw)
    except (ValidationError, TypeError, ValueError) as exc:
        raise InvalidOperation() from exc
    if not operations:
        raise InvalidOperation()
    return operations


def operation_data(operation: Operation) -> dict[str, Any]:
    """The sole conversion into persisted data, after closed-schema validation."""
    return operation.model_dump(mode="json")


def operation_label(operation_type: str) -> str:
    return operation_type


def operation_references(data: dict[str, Any]) -> dict[str, str]:
    """Audit/response safe references only; never return textual configuration.

    `connectionName`/`function`/`decision` (GuardrailSet) are schema values
    the operator themselves chose from a closed set, not tenant free-text --
    surfacing them lets a human reviewing the proposal actually see which
    guardrail is proposed, the same way `agentId` etc. do for every other
    operation. `conditions` is deliberately excluded: its `value` entries are
    tenant-chosen comparison data (an order-value threshold, an enum member,
    ...), not a closed schema choice -- the proposal review surface shows
    `decision` only, same as how `mission`/`taskText` are excluded for other
    operations."""
    refs: dict[str, str] = {}
    for key in (
        "agentId",
        "pluginId",
        "integrationId",
        "configurationRef",
        "departmentId",
        "connectionName",
        "function",
        "decision",
    ):
        value = data.get(key)
        if value is not None:
            refs[key] = str(value)
    return refs


async def target_revision(
    db: AsyncSession, operation: Operation, *, lock_for_apply: bool = False
) -> str | None:
    """Read a target's trigger-maintained revision.

    Application takes a row lock before comparing it, so an update cannot land
    between the stale check and the capability applier. Proposal creation only
    snapshots and therefore never takes a lock.

    `DepartmentCreate`/`AgentCreate` have no existing row to go stale --
    `None` here always compares equal to itself in `_is_stale`, so a create
    proposal is never rejected as stale. `apply_operation` still validates
    `AgentCreate.departmentId` exists at apply time, which is the one thing
    that actually could have changed underneath it.
    """
    if isinstance(operation, (DepartmentCreate, AgentCreate)):
        return None
    if isinstance(operation, (DepartmentUpdate, DepartmentDelete)):
        statement = select(m.Department.config_revision).where(
            m.Department.id == operation.departmentId, m.Department.deleted_at.is_(None)
        )
        changed = await db.scalar(statement.with_for_update() if lock_for_apply else statement)
        if changed is None:
            raise InvalidOperation()
        return str(changed)
    if isinstance(operation, DepartmentRestore):
        # The one target that must exist ARCHIVED, not live -- restoring a
        # department that is already live is meaningless, and a bare
        # deleted_at.is_(None) filter (every other department branch's
        # filter) would make target_revision() raise InvalidOperation for
        # every legitimate restore.
        statement = select(m.Department.config_revision).where(
            m.Department.id == operation.departmentId, m.Department.deleted_at.is_not(None)
        )
        changed = await db.scalar(statement.with_for_update() if lock_for_apply else statement)
        if changed is None:
            raise InvalidOperation()
        return str(changed)
    if isinstance(
        operation,
        (MissionSet, TriggerCreate, GuardrailSet, AgentRename, AgentLifecycleSet),
    ):
        statement = select(m.Agent.config_revision).where(
            m.Agent.id == operation.agentId, m.Agent.deleted_at.is_(None)
        )
        changed = await db.scalar(statement.with_for_update() if lock_for_apply else statement)
        if changed is None:
            raise InvalidOperation()
        return str(changed)
    if isinstance(operation, AgentDelete):
        statement = select(m.Agent.config_revision).where(
            m.Agent.id == operation.agentId, m.Agent.deleted_at.is_(None)
        )
        changed = await db.scalar(statement.with_for_update() if lock_for_apply else statement)
        if changed is None:
            raise InvalidOperation()
        return str(changed)
    if isinstance(operation, AgentRestore):
        statement = select(m.Agent.config_revision).where(
            m.Agent.id == operation.agentId, m.Agent.deleted_at.is_not(None)
        )
        changed = await db.scalar(statement.with_for_update() if lock_for_apply else statement)
        if changed is None:
            raise InvalidOperation()
        return str(changed)
    if isinstance(operation, PluginEnable):
        statement = select(m.Capa.config_revision).where(m.Capa.id == operation.pluginId)
        changed = await db.scalar(statement.with_for_update() if lock_for_apply else statement)
        if changed is None:
            raise InvalidOperation()
        return str(changed)
    statement = select(m.Integration.config_revision).where(
        m.Integration.id == operation.integrationId
    )
    changed = await db.scalar(statement.with_for_update() if lock_for_apply else statement)
    if changed is None:
        raise InvalidOperation()
    return str(changed)


async def apply_operation(db: AsyncSession, *, tenant_id: uuid.UUID, data: dict[str, Any]) -> None:
    """Apply exactly one validated operation through existing service boundaries."""
    operation = parse_operations([data])[0]
    if isinstance(operation, MissionSet):
        agent = await db.get(m.Agent, operation.agentId)
        if agent is None or agent.deleted_at is not None:
            raise InvalidOperation()
        agent.mission = operation.mission
        await db.flush()
        return
    if isinstance(operation, TriggerCreate):
        events = await list_installed_automation_events(db)
        try:
            await create_trigger(
                db,
                tenant_id=tenant_id,
                agent_id=operation.agentId,
                kind=operation.kind,
                task_text=operation.taskText,
                cron_expression=operation.cronExpression,
                event_source=operation.eventSource,
                event_type=operation.eventType,
                declared_events={(event.source, event.type) for event in events},
            )
        except SubscriptionModelNotManualOnly as exc:
            # A ChatGPT-subscription-backed agent may not be given an
            # unattended trigger (`oc8.modelrouter.subscription_guard`), and
            # the Copilot is no exception -- `operation.agentId` is arbitrary
            # and `kind` may be "cron". Reported through this module's one
            # rejection convention (`InvalidOperation`, which `apply_proposal`
            # turns into a rejected proposal and a 409) rather than a new
            # error surface, and value-free like every other InvalidOperation.
            raise InvalidOperation() from exc
        return
    if isinstance(operation, PluginEnable):
        await enable_plugin(
            db,
            tenant_id=tenant_id,
            capa_id=operation.pluginId,
            granted_permissions=operation.grantedPermissions,
        )
        return
    if isinstance(operation, DepartmentCreate):
        # Same defaults POST /departments uses (departments.py's
        # create_department): an empty tools frame plus the department-tier
        # memory grant every OTHER department gets, so an agent hired into
        # this one is not silently unable to remember anything.
        dept = m.Department(
            tenant_id=tenant_id,
            name=operation.name,
            goal=operation.goal,
            frame={
                "tools": {},
                "kbs": [],
                "memory": {"department": ["read", "write"], "company": ["read"]},
            },
            presentation={"icon": operation.icon},
        )
        db.add(dept)
        await db.flush()
        return
    if isinstance(operation, DepartmentUpdate):
        dept = await db.get(m.Department, operation.departmentId)
        if dept is None or dept.deleted_at is not None:
            raise InvalidOperation()
        if operation.name is not None:
            dept.name = operation.name
        if operation.goal is not None:
            dept.goal = operation.goal
        if operation.icon is not None:
            dept.presentation = {**(dept.presentation or {}), "icon": operation.icon}
        await db.flush()
        return
    if isinstance(operation, DepartmentDelete):
        dept = await db.get(m.Department, operation.departmentId)
        if dept is None or dept.deleted_at is not None:
            raise InvalidOperation()
        live_agents = (
            (
                await db.execute(
                    select(m.Agent).where(
                        m.Agent.department_id == operation.departmentId,
                        m.Agent.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        now = dt.datetime.now(tz=dt.UTC)
        if not live_agents:
            await db.delete(dept)
        else:
            dept.deleted_at = now
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
                else:
                    agent.deleted_at = now
        await db.flush()
        return
    if isinstance(operation, DepartmentRestore):
        dept = await db.get(m.Department, operation.departmentId)
        if dept is None or dept.deleted_at is None:
            raise InvalidOperation()
        dept.deleted_at = None
        await db.flush()
        return
    if isinstance(operation, AgentCreate):
        target_dept = await db.get(m.Department, operation.departmentId)
        if target_dept is None or target_dept.deleted_at is not None:
            raise InvalidOperation()
        # Deliberately minimal (name/department/mission only, no narrowing,
        # no model, no runtime override): the Copilot bootstraps a starting
        # point, same as everywhere else in this module -- a human fills in
        # tools/model/guardrails afterward through the normal Hire/agent-
        # detail flow, not through Copilot.
        gated = await require_hire_approval(db, tenant_id=tenant_id)
        agent = m.Agent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            department_id=operation.departmentId,
            name=operation.name,
            role_title=operation.roleTitle,
            mission=operation.mission,
            status="pending_approval" if gated else "stopped",
            trust_level="first_party",
            definition={"oc8_agent": 1, "name": operation.name, "mission": operation.mission},
        )
        db.add(agent)
        await db.flush()
        db.add(m.MemoryStore(tenant_id=tenant_id, tier="agent", owner_id=agent.id))
        await db.flush()
        return
    if isinstance(operation, AgentRename):
        agent = await db.get(m.Agent, operation.agentId)
        if agent is None or agent.deleted_at is not None:
            raise InvalidOperation()
        agent.name = operation.name
        await db.flush()
        return
    if isinstance(operation, AgentLifecycleSet):
        agent = await db.get(m.Agent, operation.agentId)
        if agent is None or agent.deleted_at is not None:
            raise InvalidOperation()
        if agent.status == "pending_approval":
            raise InvalidOperation()
        agent.status = _LIFECYCLE[operation.action]
        await db.flush()
        return
    if isinstance(operation, AgentDelete):
        agent = await db.get(m.Agent, operation.agentId)
        if agent is None or agent.deleted_at is not None:
            raise InvalidOperation()
        dependents = (
            await db.execute(
                select(func.count())
                .select_from(m.AgentRun)
                .where(m.AgentRun.agent_id == operation.agentId)
            )
        ).scalar_one()
        if dependents == 0:
            await db.delete(agent)
        else:
            agent.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        return
    if isinstance(operation, AgentRestore):
        agent = await db.get(m.Agent, operation.agentId)
        if agent is None or agent.deleted_at is None:
            raise InvalidOperation()
        try:
            await assert_manual_only_compatible(
                db, agent_id=agent.id, model_config_id=agent.model_config_id
            )
        except SubscriptionModelNotManualOnly as exc:
            raise InvalidOperation() from exc
        agent.deleted_at = None
        await db.flush()
        return
    if isinstance(operation, GuardrailSet):
        await _apply_guardrail_set(db, tenant_id=tenant_id, operation=operation)
        return
    # Integration prepare validates the opaque reference still identifies a
    # tenant-visible integration. It deliberately does not configure credentials.
    if await db.get(m.Integration, operation.integrationId) is None:
        raise InvalidOperation()


def _connection_tool_names(conn: m.McpConnection) -> frozenset[str]:
    """The real, manifest-declared function catalog for `conn` -- the same
    catalog `GET /mcp/connections/{name}/tool-names` serves the frontend, read
    through `resolve_tool_pack_connection` rather than `api/v1/mcp.py` to keep
    this service-layer module free of API route imports. Empty (never raises)
    when the plugin was removed from disk or ships no tool pack: callers must
    treat that as "no known function", the same fail-closed default as an
    unrecognised name.
    """
    cfg = conn.config if isinstance(conn.config, dict) else {}
    plugin_name = cfg.get("_plugin_name")
    connection_key = cfg.get("_connection_key")
    if not isinstance(plugin_name, str) or not isinstance(connection_key, str):
        return frozenset()
    manifest_conn = resolve_tool_pack_connection(plugin_name, connection_key)
    if manifest_conn is None:
        return frozenset()
    scopes = manifest_conn.scopes if isinstance(manifest_conn.scopes, dict) else {}
    return frozenset({*scopes.get("read", []), *scopes.get("modify", [])})


def _connection_guardrail_attributes(conn: m.McpConnection) -> list[GuardrailAttribute]:
    """This connection's declared `GuardrailAttribute`s -- the same closed
    catalog `agents_write.py`'s `_connection_guardrail_attributes` resolves
    for the free-text interpreter, read here too so a Copilot-proposed
    `with_limits` guardrail can only ever reference an attribute the CAPA
    actually declares, never an invented one."""
    cfg = conn.config if isinstance(conn.config, dict) else {}
    plugin_name = cfg.get("_plugin_name")
    connection_key = cfg.get("_connection_key")
    if not isinstance(plugin_name, str) or not isinstance(connection_key, str):
        return []
    manifest_conn = resolve_tool_pack_connection(plugin_name, connection_key)
    return manifest_conn.guardrail_attributes if manifest_conn is not None else []


async def _apply_guardrail_set(
    db: AsyncSession, *, tenant_id: uuid.UUID, operation: GuardrailSet
) -> None:
    """The one Copilot capability that touches real authorization: translates
    a single (connection, function, decision) triple into the same
    `ToolPolicy` fields the manual editor and the free-text interpreter both
    write, per the mapping this operation is built against --

        not_allowed        -> function absent from `only`
        self_sufficient     -> function in `only`, absent from `approval_actions`,
                               no conditions for this function
        with_limits          -> function in `only`, absent from `approval_actions`,
                               `conditions` set for this function
        approval_required   -> function in `only` AND in `approval_actions`
                               (unconditional -- every call needs a human)

    Never invents a new enforcement path: `authz/pdp.py`'s exact-match
    `only`/`approval_actions`/`conditions` semantics are untouched, and
    `narrowing_within_frame` (the same write-time ceiling check
    `PUT /agents/{id}/narrowing` uses) is reused verbatim so this can never
    widen an agent past its department frame. A `with_limits` decision's
    `conditions` are validated by `guardrail_interpret.parse_conditions` --
    the exact same validator the free-text interpreter uses -- so the
    Copilot cannot write a condition the manual editor's own interpret
    endpoint would refuse.
    """
    agent = await db.get(m.Agent, operation.agentId)
    if agent is None or agent.deleted_at is not None:
        raise InvalidOperation()
    dept = await db.get(m.Department, agent.department_id)
    frame = dept.frame if dept else {}

    # `credential_id.is_(None)` picks the manifest row, never a per-login row
    # sharing this same tenant-global name -- same pattern as `agents_write.py`'s
    # `set_narrowing` and `api/v1/mcp.py`'s tool-names lookup.
    conn = (
        await db.execute(
            select(m.McpConnection)
            .where(
                m.McpConnection.tenant_id == tenant_id,
                m.McpConnection.name == operation.connectionName,
                m.McpConnection.credential_id.is_(None),
            )
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if conn is None:
        raise InvalidOperation()
    tool_names = _connection_tool_names(conn)
    if operation.function not in tool_names:
        # Fail-closed: an unrecognised function must never silently produce a
        # broken or no-op guardrail.
        raise InvalidOperation()

    narrowing = dict(agent.narrowing) if isinstance(agent.narrowing, dict) else {}
    tools = dict(narrowing.get("tools", {}))
    existing_raw = tools.get(operation.connectionName)
    current = (
        pdp.ToolPolicy.from_json(existing_raw)
        if existing_raw is not None
        # No prior narrowing entry for this connection means "no additional
        # restriction" (see `effective_tool_policies`'s `no is None` branches) --
        # a freshly-created entry must start from that same permissive baseline,
        # not the ToolPolicy dataclass's all-False defaults, or introducing this
        # one guardrail would silently disable the connection outright.
        else pdp.ToolPolicy(enabled=True, read=True, modify=True)
    )

    # Only this function's own conditions are ever replaced -- every other
    # function's `with_limits` rule on this same connection must survive an
    # unrelated `GuardrailSet`, so conditions are scoped by the attribute
    # keys this function's declared `GuardrailAttribute`s actually cover
    # (mirroring the frontend's `stripConditionsForFunction`), not dropped
    # wholesale.
    function_attributes = attributes_for_function(
        _connection_guardrail_attributes(conn), operation.function
    )
    function_attribute_keys = {a.key for a in function_attributes}
    kept_conditions = tuple(
        c for c in current.conditions if c.attribute not in function_attribute_keys
    )
    if operation.decision == "with_limits":
        try:
            new_conditions = parse_conditions(
                [c.model_dump(mode="json") for c in operation.conditions], function_attributes
            )
        except GuardrailNotUnderstood as exc:
            raise InvalidOperation() from exc
        conditions = kept_conditions + tuple(new_conditions)
    else:
        if operation.conditions:
            # A non-"with_limits" decision must never carry conditions --
            # there is no ambiguity to resolve silently here.
            raise InvalidOperation()
        conditions = kept_conditions

    # Materialise `only` into an explicit allowlist the first time a function
    # is denied: an unset `only` (None) means "every tool", so removing just
    # one function requires starting from the full known catalog rather than
    # an empty set.
    base_only = set(current.only) if current.only is not None else set(tool_names)
    approval_actions = set(current.approval_actions)
    if operation.decision == "not_allowed":
        base_only.discard(operation.function)
        approval_actions.discard(operation.function)
    elif operation.decision == "approval_required":
        base_only.add(operation.function)
        approval_actions.add(operation.function)
    else:
        # self_sufficient / with_limits: the function itself is allowed;
        # `with_limits` conditions are evaluated per-call by
        # `evaluate_conditions`, never as an unconditional approval gate.
        base_only.add(operation.function)
        approval_actions.discard(operation.function)

    updated = pdp.ToolPolicy(
        enabled=current.enabled,
        read=current.read,
        modify=current.modify,
        approval_eur=current.approval_eur,
        approval_actions=frozenset(approval_actions),
        only=frozenset(base_only),
        connection_id=current.connection_id,
        conditions=conditions,
    )
    tools[operation.connectionName] = updated.to_json()
    narrowing["tools"] = tools

    violations = pdp.narrowing_within_frame(frame, narrowing)
    if violations:
        raise InvalidOperation()

    agent.narrowing = narrowing
    overridden = list(agent.narrowing_overridden_keys or [])
    if operation.connectionName not in overridden:
        overridden.append(operation.connectionName)
    agent.narrowing_overridden_keys = overridden
    await db.flush()
