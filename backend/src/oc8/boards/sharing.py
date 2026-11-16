"""Sharing a data board with a member or a department.

A share grants exactly one thing: reading and querying that one board
(`boards.repo._shared_with`). Who may share is the caller's business
(`may_delete_board`); this module checks the TARGET, writes the row, audits ids
only, and tells the members who see the board now and did not before.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from oc8 import models as m
from oc8.audit import append_event
from oc8.notifications.service import notify

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.auth import Principal


class ShareTargetInvalid(ValueError):
    """Not exactly one live member or live department of this tenant."""


class ShareNotFound(LookupError):
    """No such share on this board."""


async def board_audience(db: AsyncSession, board: m.DataBoard) -> set[uuid.UUID]:
    """Live members who see the board through a seat, `all_departments` or a
    share. Tenant-wide ROLE grants live on tokens and are not counted."""
    member = m.OrgMember
    seat = m.OrgMemberDepartment
    share = m.DataBoardShare
    dept = m.Department
    board_department_live = (
        await db.execute(
            select(dept.id).where(dept.id == board.department_id, dept.deleted_at.is_(None))
        )
    ).first() is not None
    everywhere = select(member.id).where(
        member.tenant_id == board.tenant_id, member.all_departments.is_(True)
    )
    if not board_department_live:
        # Seats in an archived department carry nothing (`_scope_from_seats`),
        # and no share counts for a board whose department is archived.
        stmt = everywhere.where(member.deleted_at.is_(None))
        return set((await db.execute(stmt)).scalars())
    seated_departments = select(share.department_id).where(
        share.board_id == board.id, share.department_id.is_not(None)
    )
    seated = (
        select(seat.member_id)
        .join(dept, dept.id == seat.department_id)
        .where(
            seat.tenant_id == board.tenant_id,
            seat.revoked_at.is_(None),
            dept.deleted_at.is_(None),
            or_(
                seat.department_id == board.department_id,
                seat.department_id.in_(seated_departments),
            ),
        )
    )
    direct = select(share.member_id).where(share.board_id == board.id, share.member_id.is_not(None))
    stmt = select(member.id).where(
        member.tenant_id == board.tenant_id,
        member.deleted_at.is_(None),
        or_(member.all_departments.is_(True), member.id.in_(seated), member.id.in_(direct)),
    )
    return set((await db.execute(stmt)).scalars())


async def _live_target(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
) -> None:
    if (member_id is None) == (department_id is None):
        raise ShareTargetInvalid("name exactly one member or one department")
    if member_id is not None:
        stmt = select(m.OrgMember.id).where(
            m.OrgMember.id == member_id,
            m.OrgMember.tenant_id == tenant_id,
            m.OrgMember.deleted_at.is_(None),
        )
    else:
        stmt = select(m.Department.id).where(
            m.Department.id == department_id,
            m.Department.tenant_id == tenant_id,
            m.Department.deleted_at.is_(None),
        )
    if (await db.execute(stmt)).first() is None:
        raise ShareTargetInvalid("no such live member or department")


def _target(share: m.DataBoardShare) -> dict[str, str]:
    if share.member_id is not None:
        return {"member_id": str(share.member_id)}
    return {"department_id": str(share.department_id)}


async def share_board(
    db: AsyncSession,
    *,
    board: m.DataBoard,
    actor_member_id: uuid.UUID,
    member_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    principal: Principal | None = None,
) -> m.DataBoardShare:
    """Share the board; an existing identical share is returned unchanged."""
    await _live_target(
        db, tenant_id=board.tenant_id, member_id=member_id, department_id=department_id
    )
    share = m.DataBoardShare
    same_target = (
        share.member_id == member_id
        if member_id is not None
        else share.department_id == department_id
    )

    async def _existing() -> m.DataBoardShare | None:
        stmt = select(share).where(share.board_id == board.id, same_target)
        return (await db.execute(stmt)).scalar_one_or_none()

    existing = await _existing()
    if existing is not None:
        return existing
    before = await board_audience(db, board)
    # ON CONFLICT DO NOTHING: a concurrent identical share that committed first
    # wins, and this call returns that row and tells nobody a second time.
    inserted = (
        await db.execute(
            insert(share)
            .values(
                tenant_id=board.tenant_id,
                board_id=board.id,
                member_id=member_id,
                department_id=department_id,
                created_by=actor_member_id,
            )
            .on_conflict_do_nothing(
                constraint="uq_data_board_share_member"
                if member_id is not None
                else "uq_data_board_share_department"
            )
            .returning(share.id)
        )
    ).scalar_one_or_none()
    row = await _existing()
    assert row is not None  # inserted here or by the winner of the race
    if inserted is None:
        return row
    after = await board_audience(db, board)
    agent = await db.get(m.Agent, board.agent_id) if board.agent_id else None
    body = f"{board.title} · {agent.name}" if agent is not None else board.title
    await append_event(
        db,
        tenant_id=board.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="board.shared",
        resource={"board_id": str(board.id), **_target(row)},
        principal=principal,
    )
    await notify(
        db,
        tenant_id=board.tenant_id,
        member_ids=sorted(after - before - {actor_member_id}),
        kind="board.available",
        subject_id=board.id,
        # The reason ("shared", not "created") is the title; the body is
        # "<board> · <agent>", as for a created board (`notifications/events.py`).
        title=("Dashboard shared with you", "Dashboard mit dir geteilt"),
        body=(body, body),
        link="/workspace",
        classification="internal",
    )
    return row


async def unshare_board(
    db: AsyncSession,
    *,
    board: m.DataBoard,
    share_id: uuid.UUID,
    actor_member_id: uuid.UUID,
    principal: Principal | None = None,
) -> None:
    """Remove one share of this board; `ShareNotFound` when there is none."""
    row = (
        await db.execute(
            select(m.DataBoardShare).where(
                m.DataBoardShare.id == share_id, m.DataBoardShare.board_id == board.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ShareNotFound(str(share_id))
    target = _target(row)
    await db.delete(row)
    await db.flush()
    await append_event(
        db,
        tenant_id=board.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="board.unshared",
        resource={"board_id": str(board.id), **target},
        principal=principal,
    )
