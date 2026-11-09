"""The agent builder's proposals (Copilot Agent Builder §3.2-§3.5).

One kind of `CopilotProposal` (`kind="agent_builder"`), not a parallel store:
created by the Copilot's `propose_agent` tool in a member's chat, shown as a
card, applied by the member or -- through an approval -- by somebody who may
create agents in that department. One open proposal per chat; a new one
supersedes the draft. Apply is one savepoint: every step lands, or none does
and the proposal names the step that refused.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Iterator
from typing import Any, Final, Literal

from sqlalchemy import Text, cast, func, literal, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.hire import create_hire_request
from oc8.audit import append_event
from oc8.auth import Principal
from oc8.authz.authority import Authority
from oc8.authz.permissions import AGENT, MANAGE, perm
from oc8.authz.scope import DepartmentScope
from oc8.chat.model_choice import chat_enabled_model_ids
from oc8.copilot.builder_ops import (
    BuilderInvalid,
    BuilderStep,
    builder_operation_data,
    parse_builder_operations,
    resolve_step,
)
from oc8.copilot.capabilities import (
    AgentCreate,
    AgentModelSwitch,
    AgentNarrowingSet,
    AgentSkillAssign,
    GuardrailSet,
    InvalidOperation,
    MissionSet,
    Operation,
    TriggerCreate,
    apply_operation,
    operation_label,
)
from oc8.copilot.models import CopilotOperation, CopilotProposal
from oc8.copilot.redaction import carries_secret_value

logger = logging.getLogger(__name__)

BUILDER_KIND: Final = "agent_builder"
#: The approval an employee's builder proposal becomes (spec §3.4).
AGENT_BUILDER_ACTION: Final = "copilot.agent_builder"
OPEN_STATUSES: Final = ("draft", "awaiting_approval")
PROPOSAL_LIFETIME: Final = dt.timedelta(days=7)


def may_create_agent(
    authority: Authority, scope: DepartmentScope, department_id: uuid.UUID
) -> bool:
    """The existing agent-write rule (`api.deps.authorize_agent_write`) as a
    question: tenant-wide `agent:manage`, or the seat's agent_manage toggle in
    exactly this department. A test pins the two to the same answers."""
    return perm(AGENT, MANAGE) in authority.tenant_wide or scope.may_manage_agents(department_id)


def expire_if_due(proposal: CopilotProposal, *, now: dt.datetime) -> bool:
    """Expire an open proposal past its time. Lazy, on every read and write --
    there is no sweeper, and none is needed: an expired card only has to say so
    the next time anybody looks."""
    if (
        proposal.status in OPEN_STATUSES
        and proposal.expires_at is not None
        and proposal.expires_at <= now
    ):
        proposal.status = "expired"
        return True
    return False


async def load_steps(db: AsyncSession, proposal: CopilotProposal) -> list[BuilderStep]:
    """The stored steps, in order, validated again exactly as when they were
    proposed: a stored row is never trusted to still be a builder proposal (two
    creates, a reordered list, a raw `agentId` all refuse here)."""
    rows = (
        (
            await db.execute(
                select(CopilotOperation)
                .where(CopilotOperation.proposal_id == proposal.id)
                .order_by(CopilotOperation.ordinal)
            )
        )
        .scalars()
        .all()
    )
    return parse_builder_operations([dict(r.configuration) for r in rows])


async def lock_builder_proposal(db: AsyncSession, proposal_id: uuid.UUID) -> CopilotProposal | None:
    """The proposal row, locked and freshly read: a second click waits here
    and then sees the first one's outcome rather than a stale identity-map copy."""
    return (
        await db.execute(
            select(CopilotProposal)
            .where(CopilotProposal.id == proposal_id, CopilotProposal.kind == BUILDER_KIND)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _live_department(db: AsyncSession, department_id: uuid.UUID) -> m.Department | None:
    dept = await db.get(m.Department, department_id)
    return dept if dept is not None and dept.deleted_at is None else None


def enabled_systems(frame: dict[str, Any] | None) -> frozenset[str]:
    """The systems (connection names) a department's frame switches on."""
    tools = (frame or {}).get("tools") or {}
    if not isinstance(tools, dict):
        return frozenset()
    return frozenset(
        name for name, cfg in tools.items() if isinstance(cfg, dict) and cfg.get("enabled")
    )


async def usable_systems(
    db: AsyncSession, *, tenant_id: uuid.UUID, department: m.Department
) -> frozenset[str]:
    """What a builder proposal may select (Ruling A12): enabled by the
    department's frame AND backed by a connected connection of that name,
    tenant-wide or in this department. A revoked connection drops out."""
    enabled = enabled_systems(department.frame)
    if not enabled:
        return frozenset()
    connected = (
        await db.execute(
            select(m.McpConnection.name).where(
                m.McpConnection.tenant_id == tenant_id,
                m.McpConnection.connected.is_(True),
                m.McpConnection.name.in_(enabled),
                or_(
                    m.McpConnection.department_id.is_(None),
                    m.McpConnection.department_id == department.id,
                ),
            )
        )
    ).scalars()
    return frozenset(connected)


def _texts(operation: Operation) -> list[str]:
    """Free text a step stores as configuration -- what the secret gate reads."""
    if isinstance(operation, AgentCreate):
        return [operation.name, operation.roleTitle, operation.mission]
    if isinstance(operation, MissionSet):
        return [operation.mission]
    if isinstance(operation, TriggerCreate):
        return [
            text
            for text in (operation.taskText, operation.eventSource, operation.eventType)
            if text is not None
        ]
    if isinstance(operation, GuardrailSet):
        return [text for c in operation.conditions for text in _leaf_texts(c.model_dump())]
    if isinstance(operation, AgentNarrowingSet):
        return list(_leaf_texts(operation.narrowing))
    return []


def _leaf_texts(value: object, key: str | None = None) -> Iterator[str]:
    """Every key and every leaf of a JSON-shaped value, and `key=value` for a
    keyed leaf -- so `{"password": "x"}` reads as `password=x` to the secret
    gate, at any depth (Ruling A20)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _leaf_texts(v, str(k))
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _leaf_texts(item, key)
    elif value is not None:
        yield str(value)
        if key is not None:
            yield f"{key}={value}"


def _create_step(steps: list[BuilderStep]) -> AgentCreate:
    create = steps[0].operation
    if not isinstance(create, AgentCreate):
        raise BuilderInvalid("missing_create")
    return create


def _narrowed_systems(operation: AgentNarrowingSet) -> list[str] | None:
    """The connection names a narrowing names, or None when it is malformed."""
    tools = operation.narrowing.get("tools", {})
    return list(tools) if isinstance(tools, dict) else None


async def check_selections(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    department: m.Department,
    steps: list[BuilderStep],
) -> None:
    """Only what the member may use (spec §3.5): chat-enabled models (U1),
    current skill versions, usable systems (A12), no credential value in stored
    text (A20), and a name nobody else has. Run when the proposal is made and
    again when it is applied -- the world may have moved."""
    create = _create_step(steps)
    for index, step in enumerate(steps):
        if any(carries_secret_value(text) for text in _texts(step.operation)):
            raise BuilderInvalid("secret_value", index)
    taken = await db.scalar(
        select(func.count())
        .select_from(m.Agent)
        .where(
            m.Agent.tenant_id == tenant_id,
            m.Agent.deleted_at.is_(None),
            func.lower(m.Agent.name) == func.lower(create.name.strip()),
        )
    )
    if taken:
        raise BuilderInvalid("name_taken", 0)
    models = await chat_enabled_model_ids(db, tenant_id=tenant_id)
    current_skills = set(
        (
            await db.execute(
                select(m.Skill.current_version_id).where(
                    m.Skill.tenant_id == tenant_id,
                    m.Skill.deleted_at.is_(None),
                    m.Skill.current_version_id.is_not(None),
                )
            )
        ).scalars()
    )
    systems = await usable_systems(db, tenant_id=tenant_id, department=department)
    for index, step in enumerate(steps):
        op = step.operation
        if isinstance(op, AgentModelSwitch) and op.modelConfigId not in models:
            raise BuilderInvalid("model_not_allowed", index)
        if isinstance(op, AgentSkillAssign) and op.skillVersionId not in current_skills:
            raise BuilderInvalid("skill_not_allowed", index)
        if isinstance(op, GuardrailSet) and op.connectionName not in systems:
            raise BuilderInvalid("connection_not_allowed", index)
        if isinstance(op, AgentNarrowingSet):
            named = _narrowed_systems(op)
            if named is None or any(name not in systems for name in named):
                raise BuilderInvalid("connection_not_allowed", index)


async def create_builder_proposal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member: m.OrgMember,
    scope: DepartmentScope,
    session_id: uuid.UUID,
    operations: object,
    now: dt.datetime,
) -> CopilotProposal:
    """Validate and store one builder proposal for `member`'s chat, superseding
    the chat's open draft. A proposal already waiting for approval is not
    replaced: the member waits for the decision (or it expires)."""
    steps = parse_builder_operations(operations)
    create = _create_step(steps)
    department = await _live_department(db, create.departmentId)
    if department is None or not scope.may_view(department.id):
        raise BuilderInvalid("department_unknown", 0)
    await check_selections(db, tenant_id=tenant_id, department=department, steps=steps)
    open_rows = (
        (
            await db.execute(
                select(CopilotProposal)
                .where(
                    CopilotProposal.tenant_id == tenant_id,
                    CopilotProposal.kind == BUILDER_KIND,
                    CopilotProposal.session_id == session_id,
                    CopilotProposal.status.in_(OPEN_STATUSES),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    superseded: list[str] = []
    for row in open_rows:
        if expire_if_due(row, now=now):
            continue
        if row.status == "awaiting_approval":
            raise BuilderInvalid("awaiting_approval")
        row.status = "superseded"
        row.revision += 1
        superseded.append(str(row.id))
    await db.flush()
    proposal = CopilotProposal(
        tenant_id=tenant_id,
        created_by=member.subject,
        kind=BUILDER_KIND,
        session_id=session_id,
        expires_at=now + PROPOSAL_LIFETIME,
        status="draft",
        result={},
    )
    try:
        # The open-builder unique index is the last word: a concurrent propose
        # in the same chat that this one's FOR UPDATE could not see yet loses.
        async with db.begin_nested():
            db.add(proposal)
            await db.flush()
    except IntegrityError:
        raise BuilderInvalid("superseded_concurrently") from None
    for ordinal, step in enumerate(steps):
        db.add(
            CopilotOperation(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                ordinal=ordinal,
                operation_type=operation_label(step.operation.type),
                configuration=builder_operation_data(step),
                target_revision=None,
            )
        )
    await db.flush()
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="agent",
        actor_id=None,
        category="copilot",
        action="proposal.created",
        resource={
            "proposal_id": str(proposal.id),
            "kind": BUILDER_KIND,
            "operation_count": len(steps),
            "superseded": superseded,
            "member_id": str(member.id),
        },
    )
    return proposal


async def _remember(
    db: AsyncSession, proposal: CopilotProposal, refused: BuilderInvalid
) -> BuilderInvalid:
    """Keep the refusal on the proposal, for the card (codes only)."""
    proposal.result = {
        **(proposal.result or {}),
        "refusal": refused.code,
        "failedStep": refused.step,
    }
    await db.flush()
    return refused


async def _member_id(db: AsyncSession, *, tenant_id: uuid.UUID, subject: str) -> uuid.UUID | None:
    """The live member behind a subject, for the audit trail (Ruling A13)."""
    return (
        (
            await db.execute(
                select(m.OrgMember.id).where(
                    m.OrgMember.tenant_id == tenant_id,
                    m.OrgMember.subject == subject,
                    m.OrgMember.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def _lock_agent_name(db: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> None:
    """Serialise applies that would create the same agent name in a tenant
    until this transaction ends, so the name check cannot race."""
    key = cast(literal(str(tenant_id)), Text) + ":" + func.lower(literal(name.strip()))
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


async def apply_builder_proposal(
    db: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    expect_status: Literal["draft", "awaiting_approval"],
    applied_by: str,
    principal: Principal | None,
    now: dt.datetime,
    approval_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Create the agent and apply every step in one savepoint, in order, with
    the ref mapped to the created id. Single-use. The CALLER has decided that
    `applied_by` may create agents in the proposal's department -- the member
    on the direct path, the approver on the approval path (spec §3.4)."""
    proposal = await lock_builder_proposal(db, proposal_id)
    if proposal is None:
        raise BuilderInvalid("not_found")
    if expire_if_due(proposal, now=now):
        await db.flush()
        raise BuilderInvalid("expired")
    if proposal.status == "applied":
        raise BuilderInvalid("already_applied")
    if proposal.status != expect_status:
        raise BuilderInvalid("not_open")
    try:
        steps = await load_steps(db, proposal)
    except BuilderInvalid as refused:
        raise await _remember(db, proposal, refused) from None
    create = _create_step(steps)
    department = await _live_department(db, create.departmentId)
    if department is None:
        raise await _remember(db, proposal, BuilderInvalid("department_unknown", 0))
    await _lock_agent_name(db, tenant_id=proposal.tenant_id, name=create.name)
    try:
        await check_selections(db, tenant_id=proposal.tenant_id, department=department, steps=steps)
    except BuilderInvalid as refused:
        raise await _remember(db, proposal, refused) from None
    refs: dict[str, uuid.UUID] = {}
    failed_at = 0
    try:
        async with db.begin_nested():
            for index, step in enumerate(steps):
                failed_at = index
                data, ref = resolve_step(builder_operation_data(step), refs)
                created = await apply_operation(db, tenant_id=proposal.tenant_id, data=data)
                if ref is not None:
                    if created is None:
                        raise InvalidOperation()
                    refs[ref] = created
    except (InvalidOperation, ValueError, SQLAlchemyError) as exc:
        # ValueError covers BuilderInvalid from resolve_step and any service
        # refusal that is not yet an InvalidOperation; SQLAlchemyError a step
        # the database refused. The savepoint is gone either way.
        failed = await _remember(db, proposal, BuilderInvalid("step_failed", failed_at))
        raise failed from exc
    # `load_steps` admits exactly one ref, defined by the create step.
    (agent_id,) = refs.values()
    agent = await db.get(m.Agent, agent_id)
    if agent is not None and agent.status == "pending_approval":
        # The tenant's hire gate held the new agent (`apply_operation` read the
        # same flag): raise its hire request here, in this transaction, exactly
        # as the agent-create route does (Ruling A11).
        await create_hire_request(db, agent=agent)
    proposal.status = "applied"
    proposal.applied_by = applied_by
    proposal.revision += 1
    kept = {k: v for k, v in (proposal.result or {}).items() if k not in ("refusal", "failedStep")}
    proposal.result = {**kept, "agentId": str(agent_id)}
    await db.flush()
    requested_by_member = await _member_id(
        db, tenant_id=proposal.tenant_id, subject=proposal.created_by
    )
    applied_by_member = await _member_id(db, tenant_id=proposal.tenant_id, subject=applied_by)
    await append_event(
        db,
        tenant_id=proposal.tenant_id,
        actor_type="operator",
        actor_id=applied_by_member,
        category="copilot",
        action="agent_builder.applied",
        resource={
            "proposal_id": str(proposal.id),
            "agent_id": str(agent_id),
            "department_id": str(create.departmentId),
            "requested_by": proposal.created_by,
            "requested_by_member_id": str(requested_by_member) if requested_by_member else None,
            "applied_by": applied_by,
            "applied_by_member_id": str(applied_by_member) if applied_by_member else None,
            "approval_id": str(approval_id) if approval_id else None,
        },
        principal=principal,
    )
    return agent_id
