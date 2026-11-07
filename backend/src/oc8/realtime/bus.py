"""Per-tenant event fan-out over Redis Pub/Sub. `publish_event` is best-effort:
a Redis failure is logged and swallowed so it never breaks a domain write."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.db.session import tenant_session
from oc8.notifications.push import send_push_for_tenant
from oc8.realtime.envelope import build_envelope

logger = logging.getLogger(__name__)

#: Shown when an approval carries no title of its own.
DEFAULT_PUSH_TITLE = "Neue Freigabe angefordert"


def channel_for(tenant_id: uuid.UUID) -> str:
    return f"oc8:events:{tenant_id}"


class EventBus:
    def __init__(self, url: str) -> None:
        self._client: redis.Redis = redis.from_url(url, decode_responses=True)
        # Fire-and-forget push-send tasks need a live reference or Python's
        # own GC can collect them mid-flight (a bare `asyncio.create_task()`
        # result that nothing holds is only weakly referenced) -- this set is
        # that reference, self-pruning via the done callback below. Tests
        # await this set directly to wait for a send deterministically rather
        # than polling.
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def publish_event(
        self, tenant_id: uuid.UUID, type_: str, data: dict[str, Any], *, source: str
    ) -> None:
        # A member's own approval (D3a) is theirs alone. The socket is
        # tenant-wide -- `ConnectionManager` fans out per tenant, with no
        # per-member delivery -- so its words stay out of the envelope: other
        # sockets learn only that some approval exists (the id, for a refetch
        # the server then scopes). The push below still carries them, to the
        # assignee's own devices only.
        assignee = data.get("assignee_member_id") if type_ == "approval.created" else None
        wire = {k: v for k, v in data.items() if k not in ("title", "detail")} if assignee else data
        env = build_envelope(tenant_id, type_, wire, source)
        try:
            await self._client.publish(channel_for(tenant_id), json.dumps(env))
        except Exception:
            logger.warning(
                "event publish failed (type=%s tenant=%s)", type_, tenant_id, exc_info=True
            )
        if type_ == "approval.created":
            approval_id = data.get("approval_id")
            if approval_id:
                task = asyncio.create_task(self._send_push(tenant_id, str(approval_id), data))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def _send_push(
        self, tenant_id: uuid.UUID, approval_id: str, data: dict[str, Any]
    ) -> None:
        # `publish_event` has no `db` of its own (its callers pass only
        # tenant_id/type_/data), so this opens a fresh tenant-scoped session
        # -- same pattern used throughout this codebase for work that runs
        # outside an existing request's session.
        try:
            async with tenant_session(tenant_id) as db:
                payload = await self._push_payload(db, approval_id, data)
                if payload is None:
                    return
                assignee = data.get("assignee_member_id")
                if assignee:
                    await send_push_for_tenant(
                        db,
                        tenant_id=tenant_id,
                        payload=payload,
                        member_id=uuid.UUID(str(assignee)),
                    )
                else:
                    await send_push_for_tenant(db, tenant_id=tenant_id, payload=payload)
        except Exception:
            logger.warning("push send failed for approval %s", approval_id, exc_info=True)

    @staticmethod
    async def _push_payload(
        db: AsyncSession, approval_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The notification body, preferring what the publisher already had.

        Every `approval.created` publisher holds the `ApprovalRequest` object
        when it publishes, so it puts `title`/`detail` straight in the
        envelope. That matters: three publishers (the in-process engine, the
        `ask_human` control tool, and the budget breach) publish BEFORE their
        caller commits, by design -- a fresh session's `db.get` would not see
        the row yet and the push would silently never fire.

        The `db.get` fallback stays for any caller that passes only
        `approval_id` (tests, and anything added later).
        """
        if "title" in data:
            return {
                "title": str(data.get("title") or "") or DEFAULT_PUSH_TITLE,
                "body": data.get("detail"),
                "url": "/workspace",
            }
        row = await db.get(m.ApprovalRequest, uuid.UUID(approval_id))
        if row is None:
            logger.warning(
                "no push for approval %s: the envelope carried no title and the row is "
                "not visible to a fresh session (publisher committed yet?)",
                approval_id,
            )
            return None
        return {
            "title": row.title or DEFAULT_PUSH_TITLE,
            "body": row.detail,
            "url": "/workspace",
        }

    async def close(self) -> None:
        await self._client.aclose()


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus(get_settings().redis_url)
    return _bus
