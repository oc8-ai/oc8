"""The explicit causal chain of a run, read on the server (never from the
model): a wake-up -> `wake_of_run_id` (the sub-run that finished) -> its
`parent_run_id` (the run that delegated) -> ... up to the run that started it
all.

`control_tools.person_token_role` inherits a web turn's role claim along it;
`connections.login.live_member_for` lends a person's own login along it only
when it ends in that person's live web turn (`causal_web_origin`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m

#: How old the person's web turn at the root of a chain may be for a run
#: further down to still count as theirs, live.
LIVE_ORIGIN_MAX_AGE: Final = dt.timedelta(hours=24)


def _link(run: m.AgentRun) -> str | None:
    ctx = run.context if isinstance(run.context, dict) else {}
    link = ctx.get("wake_of_run_id") or ctx.get("parent_run_id")
    return str(link) if link else None


async def causal_ancestors(
    db: AsyncSession, run: m.AgentRun, *, same_session: bool = False
) -> AsyncIterator[m.AgentRun]:
    """The runs up `run`'s causal chain, nearest first. Stops (silently) at a
    malformed or repeated link, a run that is gone or of another tenant, after
    `2 * MAX_DELEGATION_DEPTH + 2` hops (each delegation is a sub-run plus the
    wake-up it causes), and -- with `same_session` -- at a run of another chat
    session than `run`'s. A caller that needs the root checks that the last
    run yielded has no link of its own (`_link`)."""
    # Deferred: control_tools imports this module.
    from oc8.agent.control_tools import MAX_DELEGATION_DEPTH

    session = (run.context or {}).get("chat_session_id") if same_session else None
    current = run
    seen = {run.id}
    for _hop in range(2 * MAX_DELEGATION_DEPTH + 2):
        link = _link(current)
        try:
            next_id = uuid.UUID(link) if link else None
        except (ValueError, TypeError):
            return
        if next_id is None or next_id in seen:
            return
        seen.add(next_id)
        found = await db.get(m.AgentRun, next_id)
        if found is None or found.tenant_id != run.tenant_id:
            return
        if same_session and (found.context or {}).get("chat_session_id") != session:
            return
        current = found
        yield current


async def causal_web_origin(
    db: AsyncSession, run: m.AgentRun, person: m.OrgMember
) -> m.AgentRun | None:
    """The live web turn of `person` that `run` descends from, or None.

    The chain's root (the first run with no causal link, every hop in the
    same tenant and chat session as `run`) must be a chat turn through the
    web door the person wrote themselves (`originating_operator`), not a
    follow-up, a triggered run, a delegation or wake-up, nor one whose person
    was withheld, and at most `LIVE_ORIGIN_MAX_AGE` old. A wake-up is only
    ever a hop on the way, never the root."""
    from oc8.connections.acting import ACTING_PERSON_WITHHELD

    if not (run.context or {}).get("chat_session_id"):
        return None
    root: m.AgentRun | None = None
    async for ancestor in causal_ancestors(db, run, same_session=True):
        root = ancestor
    if root is None or _link(root) is not None:
        return None
    ctx = root.context if isinstance(root.context, dict) else {}
    if root.source != "chat" or ctx.get("door") != "web":
        return None
    if ctx.get("parent_task_id") or ctx.get("followup") or ctx.get("trigger_id"):
        return None
    if ctx.get(ACTING_PERSON_WITHHELD):
        return None
    if ctx.get("originating_operator") != person.subject or person.tenant_id != run.tenant_id:
        return None
    if root.created_at is None or root.created_at < dt.datetime.now(dt.UTC) - LIVE_ORIGIN_MAX_AGE:
        return None
    return root
