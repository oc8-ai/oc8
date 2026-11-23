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
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.metering.budget import set_budget
from oc8.models.core import DEPARTMENT_KIND_PERSONAL

#: `organization.settings` keys for the tenant defaults.
ENABLED_KEY = "personal_departments_enabled"
FRAME_KEY = "personal_frame_tools"
BUDGET_KEY = "personal_budget"

#: The seat the owner holds in their own department.
_OWNER_SEAT_ROLE = "dept_approver"


class PersonalDepartmentsDisabled(Exception):
    """The tenant has not enabled personal departments, and the member has none yet."""


class PersonalDepartmentSeatRefused(Exception):
    """A seat in a personal department for anyone but its owner."""


@dataclass(frozen=True)
class PersonalDefaults:
    enabled: bool
    frame_tools: dict[str, Any]
    soft_limit_tokens: int | None
    hard_limit_tokens: int | None


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
    return PersonalDefaults(
        enabled=enabled,
        frame_tools=copy.deepcopy(tools) if isinstance(tools, dict) else {},
        soft_limit_tokens=_limit(budget.get("soft_limit_tokens")),
        hard_limit_tokens=_limit(budget.get("hard_limit_tokens")),
    )


def personal_frame(tools: dict[str, Any]) -> dict[str, Any]:
    """A personal department's frame: the tenant's default tools, no knowledge
    bases, and the department's own memory only (no company memory)."""
    return {
        "tools": copy.deepcopy(tools),
        "kbs": [],
        "memory": {"department": ["read", "write"]},
    }


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
                seat_role=_OWNER_SEAT_ROLE,
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
