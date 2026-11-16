# backend/src/oc8/api/v1/notifications.py
"""Self-service Web Push subscription management. Every route here is
`unguarded`, not permission-gated: an operator manages only their own
device subscriptions, the same trust level as reading one's own profile
(see GET /me, api/v1/auth.py)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.auth import Principal
from oc8.authz.authority import authority_for_principal, tenant_wide_read
from oc8.authz.permissions import AGENT, VIEW, perm
from oc8.authz.scope import DepartmentScope, scope_for_principal
from oc8.boards.repo import board_notice_visible
from oc8.config import get_settings
from oc8.notifications.service import NotificationKind
from oc8.schemas.dto import (
    LocalizedTextDTO,
    NotificationDTO,
    NotificationPageDTO,
    NotificationSettingDTO,
    UnreadCountDTO,
    VapidPublicKeyDTO,
)
from oc8.schemas.requests import PushSubscribeRequest

router = APIRouter()

_UNGUARDED_REASON = (
    "manages only the caller's own device subscriptions -- the same trust "
    "level as reading one's own profile"
)


async def _member_for(db: DbSession, principal: Principal) -> m.OrgMember:
    return (await _member_and_scope(db, principal))[0]


async def _member_and_scope(
    db: DbSession, principal: Principal
) -> tuple[m.OrgMember, DepartmentScope]:
    """The caller's own `OrgMember` row, or a clean 403.

    `scope_for_principal(..., upsert=True)` raises `PermissionError` for a
    principal that cannot stand in a department at all (a plugin token; an
    agent token never reaches the operator API) -- see GET /me's identical
    catch (api/v1/auth.py:216-230) for the precedent. Push subscriptions are
    a per-human-operator concept, so such a caller gets a 403, not a 500.
    """
    try:
        member, scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this caller cannot hold a push subscription",
        ) from exc
    assert member is not None  # upsert=True raises before returning None
    return member, scope


@router.get(
    "/notifications/push/vapid-public-key",
    response_model=VapidPublicKeyDTO,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def get_vapid_public_key(principal: CurrentPrincipal) -> VapidPublicKeyDTO:
    return VapidPublicKeyDTO(public_key=get_settings().vapid_public_key)


@router.post(
    "/notifications/push/subscriptions",
    status_code=204,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def subscribe(
    body: PushSubscribeRequest, principal: CurrentPrincipal, db: DbSession
) -> Response:
    member = await _member_for(db, principal)
    # `tenant_id` is spelled out rather than left to RLS. Uniqueness on this
    # table is `(tenant_id, endpoint)` -- the same browser legitimately holds
    # one row per tenant its operator belongs to -- so the row this lookup must
    # find is precisely the one in THIS tenant. RLS already restricts the
    # session to that, but a lookup whose correctness now depends on the tenant
    # half of a composite key should say so where it is read, not two layers
    # down.
    result = await db.execute(
        select(m.PushSubscription).where(
            m.PushSubscription.tenant_id == principal.tenant_id,
            m.PushSubscription.endpoint == body.endpoint,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if existing.member_id != member.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this push endpoint is already registered to another member",
            )
        existing.p256dh = body.keys.p256dh
        existing.auth = body.keys.auth
    else:
        db.add(
            m.PushSubscription(
                tenant_id=principal.tenant_id,
                member_id=member.id,
                endpoint=body.endpoint,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
            )
        )
    await db.commit()
    return Response(status_code=204)


@router.delete(
    "/notifications/push/subscriptions",
    status_code=204,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def unsubscribe(endpoint: str, principal: CurrentPrincipal, db: DbSession) -> Response:
    member = await _member_for(db, principal)
    result = await db.execute(
        select(m.PushSubscription).where(
            m.PushSubscription.endpoint == endpoint,
            m.PushSubscription.member_id == member.id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        await db.commit()
    return Response(status_code=204)


# --- the member's inbox ----------------------------------------------------

_KINDS: tuple[str, ...] = get_args(NotificationKind)
_CURSOR_SEP = "_"
#: URL-safe (no `+` of an ISO offset), microsecond-exact.
_CURSOR_TS = "%Y%m%dT%H%M%S.%fZ"


def _dto(n: m.MemberNotification) -> NotificationDTO:
    return NotificationDTO(
        id=n.id,
        kind=n.kind,
        subject_id=n.subject_id,
        title=LocalizedTextDTO(en=n.title.get("en", ""), de=n.title.get("de", "")),
        body=LocalizedTextDTO(en=n.body.get("en", ""), de=n.body.get("de", "")),
        link=n.link,
        created_at=n.created_at,
        read=n.read_at is not None,
    )


def _cursor(n: m.MemberNotification) -> str:
    return f"{n.created_at.astimezone(dt.UTC).strftime(_CURSOR_TS)}{_CURSOR_SEP}{n.id}"


def _parse_cursor(raw: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        at, nid = raw.split(_CURSOR_SEP, 1)
        return dt.datetime.strptime(at, _CURSOR_TS).replace(tzinfo=dt.UTC), uuid.UUID(nid)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid cursor") from exc


async def _visible(
    request: Request, db: DbSession, principal: Principal
) -> tuple[m.OrgMember, ColumnElement[bool]]:
    """The caller's member row and the one place a notification's subject can
    veto its listing: a `board.available` item whose board the member can no
    longer see (`boards.repo`). A SQL predicate, so total, unread count and
    cursor pages agree with the items listed."""
    member, scope = await _member_and_scope(db, principal)
    authority = await authority_for_principal(request, db, principal)
    return member, board_notice_visible(
        scope=scope,
        tenant_wide=tenant_wide_read(authority, perm(AGENT, VIEW)),
        member_id=member.id,
    )


@router.get(
    "/notifications",
    response_model=NotificationPageDTO,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def list_notifications(
    request: Request,
    principal: CurrentPrincipal,
    db: DbSession,
    unread: bool = False,
    kind: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> NotificationPageDTO:
    member, visible = await _visible(request, db, principal)
    n = m.MemberNotification
    base = [n.member_id == member.id, visible]
    if unread:
        base.append(n.read_at.is_(None))
    if kind is not None:
        base.append(n.kind == kind)
    total = (await db.execute(select(func.count()).select_from(n).where(*base))).scalar_one()
    conds = list(base)
    if cursor:
        at, nid = _parse_cursor(cursor)
        conds.append(or_(n.created_at < at, and_(n.created_at == at, n.id < nid)))
    rows = list(
        (
            await db.execute(
                select(n).where(*conds).order_by(n.created_at.desc(), n.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return NotificationPageDTO(
        items=[_dto(r) for r in page],
        total_count=total,
        next_cursor=_cursor(page[-1]) if has_more and page else None,
    )


@router.get(
    "/notifications/unread-count",
    response_model=UnreadCountDTO,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def unread_count(
    request: Request, principal: CurrentPrincipal, db: DbSession
) -> UnreadCountDTO:
    member, visible = await _visible(request, db, principal)
    n = m.MemberNotification
    count = (
        await db.execute(
            select(func.count())
            .select_from(n)
            .where(n.member_id == member.id, n.read_at.is_(None), visible)
        )
    ).scalar_one()
    return UnreadCountDTO(count=count)


@router.post(
    "/notifications/read-all",
    status_code=204,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def read_all(principal: CurrentPrincipal, db: DbSession) -> Response:
    member = await _member_for(db, principal)
    n = m.MemberNotification
    await db.execute(
        update(n).where(n.member_id == member.id, n.read_at.is_(None)).values(read_at=func.now())
    )
    await db.commit()
    return Response(status_code=204)


@router.post(
    "/notifications/{notification_id}/read",
    status_code=204,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def mark_read(
    notification_id: uuid.UUID, principal: CurrentPrincipal, db: DbSession
) -> Response:
    member = await _member_for(db, principal)
    n = m.MemberNotification
    row = (
        await db.execute(select(n).where(n.id == notification_id, n.member_id == member.id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    if row.read_at is None:
        row.read_at = dt.datetime.now(dt.UTC)
        await db.commit()
    return Response(status_code=204)


async def _settings_for(db: DbSession, member_id: uuid.UUID) -> list[NotificationSettingDTO]:
    s = m.MemberNotificationSetting
    rows = {
        r.kind: r
        for r in (await db.execute(select(s).where(s.member_id == member_id))).scalars().all()
    }
    return [
        NotificationSettingDTO(
            kind=k,
            push_enabled=rows[k].push_enabled if k in rows else True,
            messenger_enabled=rows[k].messenger_enabled if k in rows else True,
        )
        for k in _KINDS
    ]


@router.get(
    "/notifications/settings",
    response_model=list[NotificationSettingDTO],
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def get_settings_list(
    principal: CurrentPrincipal, db: DbSession
) -> list[NotificationSettingDTO]:
    member = await _member_for(db, principal)
    return await _settings_for(db, member.id)


@router.put(
    "/notifications/settings",
    response_model=list[NotificationSettingDTO],
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def put_settings(
    body: list[NotificationSettingDTO], principal: CurrentPrincipal, db: DbSession
) -> list[NotificationSettingDTO]:
    member = await _member_for(db, principal)
    for item in body:
        stmt = insert(m.MemberNotificationSetting).values(
            tenant_id=principal.tenant_id,
            member_id=member.id,
            kind=item.kind,
            push_enabled=item.push_enabled,
            messenger_enabled=item.messenger_enabled,
        )
        await db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_member_notification_setting",
                set_={
                    "push_enabled": stmt.excluded.push_enabled,
                    "messenger_enabled": stmt.excluded.messenger_enabled,
                },
            )
        )
    # Read back BEFORE committing: a commit unbinds the tenant for the rest of
    # the request (see `require_departmental`), so a later query sees no rows.
    result = await _settings_for(db, member.id)
    await db.commit()
    return result
