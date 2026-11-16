"""Best-effort delivery of a member notification: push, messenger, realtime.

Runs as a background task started after the notifying transaction committed
(`oc8.notifications.service`). Never raises: the notification row already
exists, and a slow or failing push service or messenger must not surface
anywhere. Push and messenger text is German, the app's default -- no
per-member language is stored.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.notifications.push import send_push_to_members
from oc8.realtime.bus import get_event_bus

logger = logging.getLogger(__name__)


async def deliver(
    *,
    tenant_id: uuid.UUID,
    member_ids: Sequence[uuid.UUID],
    kind: str,
    title: tuple[str, str],
    body: tuple[str, str],
    link: str,
    classification: str,
) -> None:
    members = list(member_ids)
    title_de, body_de = title[1], body[1]
    # Each step runs in its own `tenant_session`. The tenant binding is
    # transaction-local (`set_config(..., true)`), and the push step commits
    # when it prunes a dead subscription: a step sharing that session after
    # the commit would run unbound, RLS would hide every row, and it would
    # silently send nothing. Separate sessions also keep one step's failure
    # (an aborted transaction) out of the next.
    push_to: list[uuid.UUID] = []
    message_to: list[uuid.UUID] = []
    try:
        async with tenant_session(tenant_id) as db:
            settings = {
                s.member_id: s
                for s in (
                    await db.execute(
                        select(m.MemberNotificationSetting).where(
                            m.MemberNotificationSetting.member_id.in_(members),
                            m.MemberNotificationSetting.kind == kind,
                        )
                    )
                ).scalars()
            }
        # An absent setting row means both channels are on.
        push_to = [mid for mid in members if mid not in settings or settings[mid].push_enabled]
        message_to = [
            mid for mid in members if mid not in settings or settings[mid].messenger_enabled
        ]
    except Exception:
        logger.warning("could not read notification settings (kind=%s)", kind, exc_info=True)
    if push_to:
        try:
            async with tenant_session(tenant_id) as db:
                await send_push_to_members(
                    db,
                    tenant_id=tenant_id,
                    member_ids=push_to,
                    payload={"title": title_de, "body": body_de, "url": link},
                )
        except Exception:
            logger.warning("notification push failed (kind=%s)", kind, exc_info=True)
    if message_to:
        try:
            async with tenant_session(tenant_id) as db:
                await _send_messenger(
                    db,
                    tenant_id=tenant_id,
                    member_ids=message_to,
                    text=f"{title_de}\n{body_de}",
                    classification=classification,
                )
        except Exception:
            logger.warning("notification messenger failed (kind=%s)", kind, exc_info=True)
    try:
        await get_event_bus().publish_member_event(
            tenant_id, members, "notification.created", {"kind": kind}, source="notifications"
        )
    except Exception:
        logger.warning("notification realtime event failed (kind=%s)", kind, exc_info=True)


async def _send_messenger(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_ids: Sequence[uuid.UUID],
    text: str,
    classification: str,
) -> None:
    if not member_ids:
        return
    # Imported here: the channel registry and the copilot module pull in a
    # large part of the app, which itself reaches the notification service.
    from oc8.channels import registry
    from oc8.channels.dispatch import tell_sender_gated
    from oc8.copilot import followups

    try:
        channels = await registry.channels_for_tenant(db, tenant_id=tenant_id)
    except Exception:
        logger.warning("could not load messenger channels for a notification", exc_info=True)
        return
    for channel, impl in channels.items():
        for member_id in member_ids:
            try:
                external_id = await followups.live_binding_external_id(
                    db, tenant_id=tenant_id, member_id=member_id, channel=channel
                )
                if external_id:
                    await tell_sender_gated(impl, external_id, text, classification)
            except Exception:
                logger.warning("notification messenger send failed (%s)", channel, exc_info=True)
