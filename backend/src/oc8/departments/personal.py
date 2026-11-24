"""Personal departments: one per member, created lazily on first use.

A personal department belongs to exactly one `OrgMember` (`owner_member_id`)
and is never listed alongside the shared ones (`departments/repo.py`). What it
starts with -- whether members may have one at all, its tool frame and its
token budget -- comes from tenant defaults an administrator keeps in
`organization.settings`, read the same way `copilot/followups.py::
max_active_followups` reads its own setting: a missing org row or a malformed
value reads as the default.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import ToolPolicy, narrowing_within_frame
from oc8.authz.tightening import held_frame_tools
from oc8.departments.frames import FrameToolsInvalid, cascade_department_tools_change
from oc8.departments.repo import live_department, lock_departments_owned_by
from oc8.metering.budget import get_budget, resume_budget_scope, set_budget
from oc8.models.core import DEPARTMENT_KIND_PERSONAL

#: `organization.settings` keys for the tenant defaults.
ENABLED_KEY = "personal_departments_enabled"
FRAME_KEY = "personal_frame_tools"
BUDGET_KEY = "personal_budget"
MAX_AGENTS_KEY = "personal_max_agents"

#: How many live agents a member may hire into their personal department when
#: the tenant sets nothing (or nothing valid) under `MAX_AGENTS_KEY`.
DEFAULT_MAX_AGENTS = 10

#: The seat the owner holds in their own department.
OWNER_SEAT_ROLE = "dept_approver"


class PersonalDepartmentsDisabled(Exception):
    """The tenant has not enabled personal departments, and the member has none yet."""


class PersonalDepartmentSeatRefused(Exception):
    """A seat in a personal department for anyone but its owner."""


class PersonalFrameViolation(Exception):
    """An agent narrowing in a personal department that its frame does not allow."""


class PersonalDepartmentRefused(Exception):
    """A personal department takes no further agent. Routes answer a 409 with
    `{"error": <error>}` and catch this base, so every subclass is mapped."""

    #: The `error` a route answers with (409).
    error = "personal_department_refused"


class PersonalAgentLimit(PersonalDepartmentRefused):
    """The personal department already holds the tenant's cap of live agents."""

    error = "personal_agent_limit"


class PersonalOwnerLeft(PersonalDepartmentRefused):
    """The department's owner has left: nothing may be added to or brought
    back into it, since an agent there would run with nobody to answer for it."""

    error = "owner_left"


def enforce_personal_narrowing(dept: m.Department, narrowing: dict[str, Any] | None) -> None:
    """The one check every write of an agent's narrowing in a department goes
    through. In a personal department the frame binds: every tool the narrowing
    names must be in the frame (a key the frame lacks would count as an
    agent-exclusive grant, `authz.pdp.narrowing_within_frame`), none may widen
    it, and the policies must be well formed. Raises `PersonalFrameViolation`.
    Standard and assistant departments are not checked here."""
    if dept.kind != DEPARTMENT_KIND_PERSONAL:
        return
    narrowing = narrowing or {}
    frame = dept.frame or {}
    narrowed_tools = narrowing.get("tools") or {}
    frame_tools = frame.get("tools") or {}
    if not isinstance(narrowed_tools, dict) or any(
        key not in frame_tools for key in narrowed_tools
    ):
        raise PersonalFrameViolation
    try:
        violations = narrowing_within_frame(frame, narrowing)
    except ValueError as exc:  # a malformed policy
        raise PersonalFrameViolation from exc
    if violations:
        raise PersonalFrameViolation


async def ensure_owner_present(db: AsyncSession, dept: m.Department) -> None:
    """Raise `PersonalOwnerLeft` for a department whose owner member is gone
    (soft-deleted or missing). Departments without an owner pass.

    Reads the row from the database, not the session's identity map: a caller
    that loaded the member earlier (the hire route does) would otherwise keep
    seeing it alive after an offboarding committed in between."""
    if dept.owner_member_id is None:
        return
    row = (
        await db.execute(
            select(m.OrgMember.id, m.OrgMember.deleted_at).where(
                m.OrgMember.id == dept.owner_member_id,
                m.OrgMember.tenant_id == dept.tenant_id,
            )
        )
    ).first()
    if row is None or row.deleted_at is not None:
        raise PersonalOwnerLeft


async def live_department_owner(db: AsyncSession, agent_id: uuid.UUID) -> uuid.UUID | None:
    """The owner of the agent's department, when the department is live and
    its owner's member row is too; None otherwise (a standard department, a
    deleted one, a departed owner, no such agent). Read from the database in
    one query, not the identity map, like `ensure_owner_present`."""
    owner: uuid.UUID | None = await db.scalar(
        select(m.OrgMember.id)
        .join(m.Department, m.Department.owner_member_id == m.OrgMember.id)
        .join(m.Agent, m.Agent.department_id == m.Department.id)
        .where(
            m.Agent.id == agent_id,
            m.Department.tenant_id == m.Agent.tenant_id,
            m.OrgMember.tenant_id == m.Agent.tenant_id,
            m.Department.deleted_at.is_(None),
            m.OrgMember.deleted_at.is_(None),
        )
    )
    return owner


async def ensure_personal_capacity(db: AsyncSession, dept: m.Department) -> None:
    """Raise `PersonalOwnerLeft` when the department's owner has left, and
    `PersonalAgentLimit` when a personal department holds the tenant's cap of
    live agents. Other kinds of department have neither."""
    if dept.kind != DEPARTMENT_KIND_PERSONAL:
        return
    # Lock the department row first, before the owner check and the count:
    # two hires at cap-1 would both count cap-1 and both insert, and a hire
    # checking the owner before an offboarding's commit would add an agent the
    # sweep never sees. The second of each pair waits here for the first's
    # commit (`stop_departments_owned_by` takes the same lock), then checks and
    # counts what it committed. The lock lasts until the transaction ends.
    await live_department(db, tenant_id=dept.tenant_id, department_id=dept.id, for_update=True)
    await ensure_owner_present(db, dept)
    defaults = await personal_defaults(db, tenant_id=dept.tenant_id)
    held = await live_agent_count(db, tenant_id=dept.tenant_id, department_id=dept.id)
    if held >= defaults.max_agents:
        raise PersonalAgentLimit


def mark_frame_overridden(dept: m.Department) -> None:
    """Stamp that an administrator edited a personal department's tool frame,
    so later tenant-default syncs leave it alone. Only the first edit stamps;
    other kinds of department carry no such stamp."""
    if dept.kind == DEPARTMENT_KIND_PERSONAL and dept.frame_overridden_at is None:
        dept.frame_overridden_at = dt.datetime.now(dt.UTC)


def mark_budget_overridden(dept: m.Department) -> None:
    """The budget counterpart of `mark_frame_overridden`."""
    if dept.kind == DEPARTMENT_KIND_PERSONAL and dept.budget_overridden_at is None:
        dept.budget_overridden_at = dt.datetime.now(dt.UTC)


def _readable(policy: Any) -> bool:
    """Whether a stored tool policy passes the strict read every write is held to."""
    if not isinstance(policy, dict):
        return False
    try:
        ToolPolicy.from_json(dict(policy), strict=True)
    except (KeyError, TypeError, ValueError):
        return False
    return True


async def lock_owned_frame(db: AsyncSession, dept: m.Department) -> None:
    """For a department with an owner, lock its row and re-read it before a
    frame write: the held split must be computed against the frame as last
    committed, or a save computed against an older frame would count undoing
    a concurrent tightening as a tightening of its own. The lock lasts until
    the transaction ends. Standard departments are left as they are."""
    if dept.owner_member_id is not None:
        # The re-read overwrites the loaded instance: flush first, so a change
        # the caller made to it (follow-default clears its stamps) is kept.
        await db.flush()
        await live_department(db, tenant_id=dept.tenant_id, department_id=dept.id, for_update=True)


async def has_agents(db: AsyncSession, dept: m.Department) -> bool:
    """Whether the department holds any agent, archived ones included: what
    `apply_held_tools` holds a loosening for. An archived agent counts, since a
    restore only clips it toward a tighter frame, never asks the owner."""
    found = await db.execute(
        select(m.Agent.id)
        .where(m.Agent.tenant_id == dept.tenant_id, m.Agent.department_id == dept.id)
        .limit(1)
    )
    return found.first() is not None


def apply_held_tools(
    dept: m.Department, requested: dict[str, Any], *, live_agents: bool
) -> dict[str, Any]:
    """The frame tools to write now for an administrator's `requested` write.

    `live_agents` is `has_agents` (archived agents count), and the caller
    holds the row lock (`lock_owned_frame`). A department with an owner and
    agents takes what only tightens at once and holds what loosens
    (`authz.tightening.held_frame_tools`): the
    request then waits in `pending_frame_tools` until the owner confirms it,
    and the applied tools are returned. Anywhere else the request applies as
    it is and nothing waits. Either way the newest write replaces anything an
    earlier one left waiting.

    On a department with an owner every requested policy must pass the
    strict read, or this raises `FrameToolsInvalid` (`frame_tools_malformed`).
    A stored policy that does not is treated as absent, so the requested one
    for that key is held when it is enabled. Fields the policy does not read
    (`default_connection_id`) are not judged: they apply at once, and the
    owner's confirmation then asks again (`connections.confirmation`)."""
    if dept.owner_member_id is None:
        # A standard department: exactly as before, nothing is held.
        return copy.deepcopy(dict(requested))
    malformed = sorted(key for key, policy in requested.items() if not _readable(policy))
    if malformed:
        raise FrameToolsInvalid({"error": "frame_tools_malformed", "keys": malformed})
    if not live_agents:
        dept.pending_frame_tools = None
        return copy.deepcopy(dict(requested))
    current = {
        key: policy
        for key, policy in ((dept.frame or {}).get("tools") or {}).items()
        if _readable(policy)
    }
    now, pending = held_frame_tools(current, requested)
    for key, entry in now.items():
        if entry == requested[key]:
            continue
        # A meet: its `to_json` carries only what the policy reads. Keep the
        # request's other fields (they apply at once, see above).
        extra = {k: v for k, v in requested[key].items() if k not in entry}
        now[key] = {**copy.deepcopy(extra), **entry}
    dept.pending_frame_tools = pending
    return now


@dataclass(frozen=True)
class PersonalDefaults:
    enabled: bool
    frame_tools: dict[str, Any]
    soft_limit_tokens: int | None
    hard_limit_tokens: int | None
    max_agents: int = DEFAULT_MAX_AGENTS


def _max_agents(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return DEFAULT_MAX_AGENTS


def _limit(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


async def personal_defaults(db: AsyncSession, *, tenant_id: uuid.UUID) -> PersonalDefaults:
    """The tenant's defaults for personal departments.

    The feature is an administrator's opt-in: with the enabled key absent (or
    anything but `true`) it reads as disabled. A missing org row or a
    malformed value reads as the default -- disabled, no tools, no limits.
    """
    org = await db.get(m.Organization, tenant_id)
    settings: dict[str, Any] = org.settings if org is not None and org.settings else {}
    enabled = settings.get(ENABLED_KEY) is True
    tools = settings.get(FRAME_KEY)
    budget = settings.get(BUDGET_KEY)
    if not isinstance(budget, dict):
        budget = {}
    soft = _limit(budget.get("soft_limit_tokens"))
    hard = _limit(budget.get("hard_limit_tokens"))
    if soft is not None and hard is not None and soft > hard:
        # The admin route refuses this pair (422), so only a hand edit or an
        # older writer can store it. Applying it would stamp a budget whose
        # soft warning never fires before the hard stop; read it as malformed,
        # i.e. no limits, like any other malformed value here.
        soft = hard = None
    return PersonalDefaults(
        enabled=enabled,
        frame_tools=copy.deepcopy(tools) if isinstance(tools, dict) else {},
        soft_limit_tokens=soft,
        hard_limit_tokens=hard,
        max_agents=_max_agents(settings.get(MAX_AGENTS_KEY)),
    )


async def update_personal_settings(
    db: AsyncSession, *, tenant_id: uuid.UUID, updates: dict[str, Any]
) -> m.Organization | None:
    """Write tenant defaults into `organization.settings`. Reassigns the dict,
    never mutates it in place, so SQLAlchemy tracks the JSONB change (as
    `agents.hire.set_require_hire_approval` does). `None` if the org row is
    missing. Flushes only."""
    org = await db.get(m.Organization, tenant_id)
    if org is None:
        return None
    org.settings = {**(org.settings or {}), **updates}
    await db.flush()
    return org


def personal_frame(tools: dict[str, Any]) -> dict[str, Any]:
    """A personal department's frame: the tenant's default tools, no knowledge
    bases, and the department's own memory only (no company memory)."""
    return {
        # Closed: a tool the frame lacks cannot be granted by an agent's own
        # narrowing (`authz.pdp.effective_tool_policies`). Every writer of a
        # personal frame keeps it: they copy the dict or call this function.
        "closed": True,
        "tools": copy.deepcopy(tools),
        "kbs": [],
        "memory": {"department": ["read", "write"]},
    }


async def stamp_budget_override(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> None:
    """`mark_budget_overridden` for the department with this id, if it exists."""
    dept = (
        await db.execute(
            select(m.Department).where(
                m.Department.tenant_id == tenant_id, m.Department.id == department_id
            )
        )
    ).scalar_one_or_none()
    if dept is not None:
        mark_budget_overridden(dept)


async def live_agent_count(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> int:
    """How many live (not soft-deleted) agents the department holds -- what the
    tenant's `MAX_AGENTS_KEY` cap counts."""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(m.Agent)
                .where(
                    m.Agent.tenant_id == tenant_id,
                    m.Agent.department_id == department_id,
                    m.Agent.deleted_at.is_(None),
                )
            )
        ).scalar_one()
    )


async def owned_personal_department(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> m.Department | None:
    """The member's live personal department, if they have one."""
    return (
        (
            await db.execute(
                select(m.Department).where(
                    m.Department.tenant_id == tenant_id,
                    m.Department.kind == DEPARTMENT_KIND_PERSONAL,
                    m.Department.owner_member_id == member_id,
                    m.Department.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def get_or_create_personal_department(
    db: AsyncSession, *, member: m.OrgMember
) -> m.Department:
    """The member's personal department, created from the tenant defaults on
    first use. An existing one is returned even when the feature has since been
    disabled; creating one when it is disabled raises
    `PersonalDepartmentsDisabled`. Flushes only -- the caller owns the commit.
    """
    tenant_id = member.tenant_id
    existing = await owned_personal_department(db, tenant_id=tenant_id, member_id=member.id)
    if existing is not None:
        return existing

    defaults = await personal_defaults(db, tenant_id=tenant_id)
    if not defaults.enabled:
        raise PersonalDepartmentsDisabled

    # Imported here, not at module level, so `workspace.members` may import
    # this module's exceptions without a cycle.
    from oc8.workspace.members import grant_seat

    # Create inside a SAVEPOINT, exactly as `agent.assistant.
    # get_or_create_assistant` does: two tabs hiring at once race here, and the
    # partial unique index `uq_department_personal_owner` turns the loser into
    # an IntegrityError. Only the savepoint unwinds -- taking the loser's
    # department and seat with it -- so the outer transaction, and its RLS
    # tenant binding, stays alive for the re-select of the winner.
    try:
        async with db.begin_nested():
            dept = m.Department(
                tenant_id=tenant_id,
                name=member.display_name or member.subject,
                goal="",
                kind=DEPARTMENT_KIND_PERSONAL,
                owner_member_id=member.id,
                frame=personal_frame(defaults.frame_tools),
                presentation={"icon": "user"},
            )
            db.add(dept)
            await db.flush()
            await grant_seat(
                db,
                tenant_id=tenant_id,
                member_id=member.id,
                department_id=dept.id,
                seat_role=OWNER_SEAT_ROLE,
                agent_manage=True,
                granted_by=member.id,
            )
    except IntegrityError:
        winner = await owned_personal_department(db, tenant_id=tenant_id, member_id=member.id)
        if winner is None:  # pragma: no cover - not the index we raced on
            raise
        return winner

    if defaults.soft_limit_tokens is not None or defaults.hard_limit_tokens is not None:
        await set_budget(
            db,
            tenant_id=tenant_id,
            department_id=dept.id,
            soft_limit_tokens=defaults.soft_limit_tokens,
            hard_limit_tokens=defaults.hard_limit_tokens,
        )
    return dept


# --- Administration: the "Personal" list and re-syncing the tenant defaults ---
#
# Every load below is unscoped by design: the callers are admin routes behind
# tenant-wide `department:manage` / `budget:manage`, which no seat can hold.


@dataclass(frozen=True)
class PersonalDepartmentSummary:
    """What an administrator may see of a personal department: metadata only.
    No frame, no narrowing, no agent names -- those belong to the owner."""

    id: uuid.UUID
    owner_member_id: uuid.UUID
    owner_name: str
    owner_active: bool
    agent_count: int
    follows_default_frame: bool
    follows_default_budget: bool
    created_at: dt.datetime
    #: An administrator's loosening of the frame tools waits for the owner.
    pending_confirmation: bool = False


async def personal_department_summaries(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID | None = None
) -> list[PersonalDepartmentSummary]:
    """Every live (not soft-deleted) personal department, oldest first, or just
    the one with `department_id`. A department whose owner has left is still
    listed: the offboarding hook keeps it so administrators see its metadata."""
    agents = (
        select(m.Agent.department_id, func.count().label("n"))
        .where(m.Agent.tenant_id == tenant_id, m.Agent.deleted_at.is_(None))
        .group_by(m.Agent.department_id)
        .subquery()
    )
    stmt = (
        select(
            m.Department,
            m.OrgMember.display_name,
            m.OrgMember.id,
            m.OrgMember.deleted_at,
            agents.c.n,
        )
        .outerjoin(
            m.OrgMember,
            (m.OrgMember.id == m.Department.owner_member_id)
            & (m.OrgMember.tenant_id == m.Department.tenant_id),
        )
        .outerjoin(agents, agents.c.department_id == m.Department.id)
        .where(
            m.Department.tenant_id == tenant_id,
            m.Department.kind == DEPARTMENT_KIND_PERSONAL,
            m.Department.deleted_at.is_(None),
        )
        .order_by(m.Department.created_at, m.Department.id)
    )
    if department_id is not None:
        stmt = stmt.where(m.Department.id == department_id)
    out: list[PersonalDepartmentSummary] = []
    for dept, display_name, member_id, member_deleted_at, n in (await db.execute(stmt)).all():
        assert dept.owner_member_id is not None  # ck: personal <=> owner
        out.append(
            PersonalDepartmentSummary(
                id=dept.id,
                owner_member_id=dept.owner_member_id,
                owner_name=display_name or "",
                owner_active=member_id is not None and member_deleted_at is None,
                agent_count=int(n or 0),
                follows_default_frame=dept.frame_overridden_at is None,
                follows_default_budget=dept.budget_overridden_at is None,
                created_at=dept.created_at,
                pending_confirmation=dept.pending_frame_tools is not None,
            )
        )
    return out


async def _following_departments(
    db: AsyncSession, *, tenant_id: uuid.UUID, following: Literal["frame", "budget"]
) -> list[m.Department]:
    """The personal departments a re-sync of one default reaches: live, owned
    by a member who has not left, and not stamped as overriding that default."""
    stamp = (
        m.Department.frame_overridden_at
        if following == "frame"
        else m.Department.budget_overridden_at
    )
    return list(
        (
            await db.execute(
                select(m.Department)
                .join(
                    m.OrgMember,
                    (m.OrgMember.id == m.Department.owner_member_id)
                    & (m.OrgMember.tenant_id == m.Department.tenant_id),
                )
                .where(
                    m.Department.tenant_id == tenant_id,
                    m.Department.kind == DEPARTMENT_KIND_PERSONAL,
                    m.Department.deleted_at.is_(None),
                    m.OrgMember.deleted_at.is_(None),
                    stamp.is_(None),
                )
                .order_by(m.Department.created_at, m.Department.id)
                # Locked (and re-read) in one fixed order: each is then
                # written against its frame as last committed.
                .with_for_update(of=m.Department)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _live_agents(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_ids: list[uuid.UUID]
) -> list[m.Agent]:
    if not department_ids:
        return []
    return list(
        (
            await db.execute(
                select(m.Agent).where(
                    m.Agent.tenant_id == tenant_id,
                    m.Agent.department_id.in_(department_ids),
                    m.Agent.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def default_frame_agents(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[m.Agent]:
    """The live agents of every personal department a default-frame re-sync
    reaches -- the "M" of the default frame's "N of M agents deviate"."""
    depts = await _following_departments(db, tenant_id=tenant_id, following="frame")
    return await _live_agents(db, tenant_id=tenant_id, department_ids=[d.id for d in depts])


async def _apply_frame(
    db: AsyncSession, *, dept: m.Department, new_tools: dict[str, Any], hold: bool = True
) -> None:
    """Write `new_tools` as the department's frame tools and cascade them to its
    agents. With `hold` (every administrator's write) a loosening waits for the
    owner (`apply_held_tools`); without it (the owner's own confirmation)
    `new_tools` applies as it is and nothing is left waiting."""
    await lock_owned_frame(db, dept)
    old_tools = dict((dept.frame or {}).get("tools", {}))
    agents = await _live_agents(db, tenant_id=dept.tenant_id, department_ids=[dept.id])
    if hold:
        applied = apply_held_tools(dept, new_tools, live_agents=await has_agents(db, dept))
    else:
        applied = copy.deepcopy(new_tools)
        dept.pending_frame_tools = None
    # Only the tools follow the tenant's default: every other key of this
    # department's frame (what it emits, takes in, its default connection)
    # stays, and the frame stays closed (`personal_frame`).
    frame = copy.deepcopy(dict(dept.frame or {}))
    frame["tools"] = copy.deepcopy(applied)
    frame["closed"] = True
    dept.frame = frame
    await cascade_department_tools_change(
        db, old_tools=old_tools, new_tools=applied, agents=agents, clip_to_frame=True
    )


async def _apply_budget(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID,
    soft_limit_tokens: int | None,
    hard_limit_tokens: int | None,
) -> None:
    if soft_limit_tokens is not None or hard_limit_tokens is not None:
        await set_budget(
            db,
            tenant_id=tenant_id,
            department_id=department_id,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
        )
        return
    # A default with no limits means no budget row at all, the same as a
    # department created while the default had none.
    existing = await get_budget(db, tenant_id=tenant_id, department_id=department_id)
    if existing is not None:
        await db.delete(existing)
        await db.flush()
        await resume_budget_scope(db, tenant_id=tenant_id, department_id=department_id)


# The two re-syncs run synchronously inside the admin's request transaction and
# cost O(personal departments) -- each one loads and cascades its own agents.
# Fine at today's tenant sizes; a batch job is a later concern.


async def resync_frames(
    db: AsyncSession, *, tenant_id: uuid.UUID, new_tools: dict[str, Any]
) -> int:
    """Re-apply the default frame to every personal department that follows it,
    cascading each changed tool's `enabled` to its agents exactly like
    `PUT /departments/{id}/tools` (an agent's own override of a key is kept).
    Returns how many departments were re-synced. Flushes only."""
    depts = await _following_departments(db, tenant_id=tenant_id, following="frame")
    for dept in depts:
        await _apply_frame(db, dept=dept, new_tools=new_tools)
    await db.flush()
    return len(depts)


async def resync_budgets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    soft_limit_tokens: int | None,
    hard_limit_tokens: int | None,
) -> int:
    """Re-apply the default budget to every personal department that follows
    it; with no limits, their budget rows are removed. Returns how many
    departments were re-synced. Flushes only."""
    depts = await _following_departments(db, tenant_id=tenant_id, following="budget")
    for dept in depts:
        await _apply_budget(
            db,
            tenant_id=tenant_id,
            department_id=dept.id,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
        )
    return len(depts)


async def follow_default(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> m.Department | None:
    """Clear both override stamps of a live personal department and re-apply
    the tenant's current default frame and budget to it. `None` when no live
    personal department has this id, or its owner has left (the scope of the
    re-syncs, `_following_departments`). Flushes only."""
    dept = (
        await db.execute(
            select(m.Department)
            .join(
                m.OrgMember,
                (m.OrgMember.id == m.Department.owner_member_id)
                & (m.OrgMember.tenant_id == m.Department.tenant_id),
            )
            .where(
                m.Department.tenant_id == tenant_id,
                m.Department.id == department_id,
                m.Department.kind == DEPARTMENT_KIND_PERSONAL,
                m.Department.deleted_at.is_(None),
                m.OrgMember.deleted_at.is_(None),
            )
            .with_for_update(of=m.Department)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if dept is None:
        return None
    defaults = await personal_defaults(db, tenant_id=tenant_id)
    dept.frame_overridden_at = None
    dept.budget_overridden_at = None
    await _apply_frame(db, dept=dept, new_tools=defaults.frame_tools)
    await _apply_budget(
        db,
        tenant_id=tenant_id,
        department_id=dept.id,
        soft_limit_tokens=defaults.soft_limit_tokens,
        hard_limit_tokens=defaults.hard_limit_tokens,
    )
    await db.flush()
    return dept


async def stop_departments_owned_by(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> int:
    """Archive every live agent of the departments a leaving member owns, and
    return how many there were.

    Keys on `owner_member_id` alone -- no kind and no `deleted_at` predicate --
    so an archived department is covered too, and so is any kind that later
    gains an owner. The departments stay, as metadata. The agents are archived
    (`deleted_at`, kept as rows: a run's `agent_id` must keep pointing at one),
    because `stopped` is only the idle status and the worker still runs queued
    work for it; an archived agent is refused by the executor's archived guard.
    Their tool bindings are revoked, and `agents.retire.retire_agent` ends the
    in-flight work: open approvals expire, unfinished runs get the
    `agent_archived` cancellation (a queued run is skipped at pickup, a running
    one stops at its next step boundary, a parked one is woken and ended).
    add/flush only: the caller owns the commit.
    """
    from oc8.agents.retire import retire_agent
    from oc8.audit.chain import append_event
    from oc8.runtime.cancellation import AGENT_ARCHIVED

    # Lock the departments before reading their agents: a concurrent hire
    # (`ensure_personal_capacity`, same lock) either commits first, and its
    # agent is selected and archived below, or waits and then sees the owner
    # gone. Without the lock a hire committing after this select would leave
    # a live agent behind.
    owned = await lock_departments_owned_by(db, tenant_id=tenant_id, member_id=member_id)
    agent_ids = list(
        (
            await db.execute(
                select(m.Agent.id).where(
                    m.Agent.tenant_id == tenant_id,
                    m.Agent.department_id.in_(owned),
                    m.Agent.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    # Every agent of the departments, archived ones included: a trigger or a
    # binding left on any of them would outlive the owner.
    all_agents = select(m.Agent.id).where(
        m.Agent.tenant_id == tenant_id, m.Agent.department_id.in_(owned)
    )
    # Triggers first (see `retire_agent`): the lock keeps a scheduler from
    # starting a run between here and the commit. Every trigger of these
    # agents, the member's chat-session follow-ups included on purpose
    # (`retire_agent` skips those: they belong to the member, who is leaving).
    await db.execute(
        update(m.Trigger)
        .where(m.Trigger.tenant_id == tenant_id, m.Trigger.agent_id.in_(all_agents))
        .values(enabled=False, last_skip_reason=AGENT_ARCHIVED)
    )
    await db.execute(
        delete(m.AgentToolAccess).where(
            m.AgentToolAccess.tenant_id == tenant_id, m.AgentToolAccess.agent_id.in_(all_agents)
        )
    )
    if not agent_ids:
        return 0
    await db.execute(
        update(m.Agent)
        .where(m.Agent.tenant_id == tenant_id, m.Agent.id.in_(agent_ids))
        .values(deleted_at=dt.datetime.now(tz=dt.UTC))
    )
    for agent_id in agent_ids:
        await retire_agent(
            db,
            agent_id=agent_id,
            hard_delete=False,
            principal=None,
            resource_extra={"member_id": str(member_id), "offboarded": True},
        )
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="system",
        actor_id=None,
        category="department",
        action="department.owner_offboarded",
        resource={"member_id": str(member_id), "agents_stopped": len(agent_ids)},
        decision="allow",
    )
    await db.flush()
    return len(agent_ids)
