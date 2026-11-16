"""Member notifications: one inbox row per member, delivered after commit.

`notify` writes the rows inside the caller's transaction and only STAGES the
delivery (push, messenger, realtime) on the session. An `after_commit`
listener on the sync `Session` class -- registered once, at import, the same
way `oc8.audit.chain` registers its `before_commit` listener -- starts the
staged deliveries as background tasks once the root transaction has
committed. A rollback discards them, and so does rolling back a SAVEPOINT
they were staged inside: a decision that fails after `notify()` must not have
told anybody anything.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import event, func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from oc8 import models as m
from oc8.notifications import delivery

logger = logging.getLogger(__name__)

NotificationKind = Literal["board.available", "run.failed", "approval.decided"]
#: `(en, de)`.
Localized = tuple[str, str]

_PENDING_KEY = "oc8_pending_notifications"

#: Live references to running deliveries: a bare `asyncio.create_task()` result
#: that nothing holds can be garbage-collected mid-flight. Self-pruning via the
#: done callback; tests await this set to wait for deliveries deterministically.
_background_tasks: set[asyncio.Task[None]] = set()


@dataclass(eq=False)
class _Staged:
    tenant_id: uuid.UUID
    member_ids: list[uuid.UUID]
    kind: str
    title: Localized
    body: Localized
    link: str
    classification: str
    # The innermost SAVEPOINT open when staged (None: the root transaction).
    scope: SessionTransaction | None


async def notify(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_ids: Iterable[uuid.UUID],
    kind: NotificationKind,
    subject_id: uuid.UUID | None,
    title: Localized,
    body: Localized,
    link: str,
    classification: str = "internal",
) -> None:
    """Write one unread notification per member and stage its delivery.

    A member who still has an unread notification of this `kind` about this
    `subject_id` gets that row refreshed (newer `created_at`, new text) rather
    than a second one -- repeated failures of one agent collapse into one
    entry, and the partial unique index makes that hold under concurrency.
    """
    members = list(dict.fromkeys(member_ids))
    if not members:
        return
    title_json = {"en": title[0], "de": title[1]}
    body_json = {"en": body[0], "de": body[1]}
    stmt = insert(m.MemberNotification).values(
        [
            {
                "tenant_id": tenant_id,
                "member_id": member_id,
                "kind": kind,
                "subject_id": subject_id,
                "title": title_json,
                "body": body_json,
                "link": link,
            }
            for member_id in members
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["member_id", "kind", "subject_id"],
        index_where=text("read_at IS NULL"),
        set_={
            "created_at": func.now(),
            "title": stmt.excluded.title,
            "body": stmt.excluded.body,
            "link": stmt.excluded.link,
        },
    )
    await db.execute(stmt)
    sync = db.sync_session
    pending: list[_Staged] = sync.info.setdefault(_PENDING_KEY, [])
    pending.append(
        _Staged(
            tenant_id=tenant_id,
            member_ids=members,
            kind=kind,
            title=title,
            body=body,
            link=link,
            classification=classification,
            scope=sync.get_nested_transaction(),
        )
    )


def _start(item: _Staged) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no event loop to deliver a %s notification on", item.kind)
        return
    task = loop.create_task(
        delivery.deliver(
            tenant_id=item.tenant_id,
            member_ids=item.member_ids,
            kind=item.kind,
            title=item.title,
            body=item.body,
            link=item.link,
            classification=item.classification,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@event.listens_for(Session, "after_commit")
def _deliver_after_commit(session: Session) -> None:
    # after_commit also fires when a SAVEPOINT is released. That is not the
    # commit: the deliveries stay staged until the root transaction commits.
    if session.in_nested_transaction():
        return
    pending: list[_Staged] | None = session.info.pop(_PENDING_KEY, None)
    for item in pending or ():
        _start(item)


@event.listens_for(Session, "after_soft_rollback")
def _drop_rolled_back(session: Session, previous_transaction: SessionTransaction) -> None:
    pending: list[_Staged] | None = session.info.get(_PENDING_KEY)
    if not pending:
        return
    if not previous_transaction.nested:
        pending.clear()
        return

    def _inside(scope: SessionTransaction | None) -> bool:
        while scope is not None:
            if scope is previous_transaction:
                return True
            scope = scope.parent
        return False

    pending[:] = [p for p in pending if not _inside(p.scope)]


@event.listens_for(Session, "after_transaction_end")
def _clear_on_end(session: Session, transaction: SessionTransaction) -> None:
    # The root transaction is over (committed, rolled back, or closed): nothing
    # staged in it may be delivered any more.
    if transaction.parent is None:
        session.info.pop(_PENDING_KEY, None)


#: A read notification is kept this long, then pruned. Unread ones are never.
READ_RETENTION = dt.timedelta(days=90)


async def prune_read_notifications() -> int:
    """Delete notifications read more than `READ_RETENTION` ago, in every
    tenant. One tick of the worker's housekeeping timer; never raises."""
    from sqlalchemy import delete

    from oc8.db.session import tenant_session
    from oc8.triggers.scheduler import list_active_tenant_ids

    try:
        tenants = await list_active_tenant_ids()
    except Exception:
        logger.warning("notifications: could not list tenants; skipping prune", exc_info=True)
        return 0
    deleted = 0
    cutoff = dt.datetime.now(dt.UTC) - READ_RETENTION
    for tenant_id in tenants:
        try:
            async with tenant_session(tenant_id) as db:
                result = await db.execute(
                    delete(m.MemberNotification).where(
                        m.MemberNotification.tenant_id == tenant_id,
                        m.MemberNotification.read_at.is_not(None),
                        m.MemberNotification.read_at < cutoff,
                    )
                )
            deleted += getattr(result, "rowcount", 0) or 0
        except Exception:
            logger.warning("notifications: prune failed for tenant %s", tenant_id, exc_info=True)
    return deleted
