"""After an approved call ran: remember which record it produced on the
approval itself (spec §5 item 5), so the card says "Created: S00043 ↗"."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.approvals.repo import approved_awaiting_result

logger = logging.getLogger(__name__)


async def note_approved_result(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID | None,
    tool_names: Sequence[str],
    arguments: Mapping[str, Any],
    urls: Sequence[str],
) -> None:
    """Set `result_url` on the approval of exactly this call, if there is one.

    Matched on tool and arguments, the same key the approval was raised for:
    a run can hold several approvals, and only the one for this call produced
    this record. `tool_names` takes both the advertised and the routed name,
    since a routed toolset raises under the name the model saw.

    Never raises: it runs after a call that already succeeded, and a missing
    "Created" link must not turn that into a failed call. The savepoint keeps
    a failed statement from poisoning the caller's transaction.
    """
    if task_id is None or not urls:
        return
    try:
        async with db.begin_nested():
            for approval in await approved_awaiting_result(
                db, tenant_id=tenant_id, task_id=task_id
            ):
                payload = approval.payload or {}
                if payload.get("tool") in tool_names and payload.get("arguments") == dict(
                    arguments
                ):
                    approval.result_url = urls[0]
                    return
    except Exception:
        logger.warning("the approved call's record link could not be noted", exc_info=True)
