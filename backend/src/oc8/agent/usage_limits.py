"""Usage limits, counted by the core: "at most 50 mails a day".

A mission line ("send no more than 50 mails a day") is a request to the model;
a model having a bad day, or a mailbox full of strangers writing in, sends the
51st anyway. A guardrail may therefore carry `limits`
(`authz.pdp.UsageLimit`): which tools, how many calls, per UTC day
or hour. The count belongs to the agent and the connection (tool key), across
all of its runs and tasks.

Three properties this deliberately has:

* **The database decides.** One conditional upsert checks and increments in a
  single statement (`take`): two parallel runs at 49/50 cannot both get the
  50th call, because the second one's upsert waits on the first one's row
  lock and then sees 50.
* **Calls that reached the tool count, even if it reported an error.** The
  count is taken right before the call -- after every other gate let it
  through, so a call oc8 refused or held never counts -- and given back
  (`give_back`) only when the call provably never reached the tool: a replay,
  a call waiting for an answer, a connection that could not be opened. A call
  that timed out, raised or returned an error keeps its count: the far system
  may have acted (sent the mail) before it failed. A call an operator approved
  counts when it runs.
* **Calendar windows.** A day is a UTC calendar day, an hour a UTC clock hour,
  so "resets at 00:00 UTC" is a sentence the model and the admin can act on.

No vendor knowledge: which tools a limit names is the guardrail's business
(a capa's preset or an admin's edit), never this module's.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import UsageLimit
from oc8.db.base import uuid7

logger = logging.getLogger(__name__)

_PERIOD_LENGTH = {"day": dt.timedelta(days=1), "hour": dt.timedelta(hours=1)}
_PERIOD_WORD = {"day": "Daily", "hour": "Hourly"}
#: Windows older than this are pruned. Longer than a day, so an admin looking
#: at "today" just after midnight still finds nothing surprising missing.
KEEP_WINDOWS_FOR = dt.timedelta(days=2)


def window_start(per: str, now: dt.datetime) -> dt.datetime:
    """The start of the UTC window `now` falls in."""
    utc = now.astimezone(dt.UTC)
    if per == "hour":
        return utc.replace(minute=0, second=0, microsecond=0)
    return utc.replace(hour=0, minute=0, second=0, microsecond=0)


def window_end(per: str, start: dt.datetime) -> dt.datetime:
    return start + _PERIOD_LENGTH[per]


def limit_refusal(limit: UsageLimit, now: dt.datetime) -> str:
    """What the model is told instead of the call. Says which limit, and when
    it resets -- a bare denial reads as a broken tool and gets retried."""
    resets = window_end(limit.per, window_start(limit.per, now))
    return (
        f"ERROR: {_PERIOD_WORD[limit.per]} limit of {limit.max} reached for "
        f"{'/'.join(limit.tools)}; resets at {resets:%H:%M} UTC. This is a policy "
        "decision, not a tool error -- do not retry it now or work around it with "
        "another tool."
    )


async def take(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_key: str,
    limit: UsageLimit,
    now: dt.datetime,
) -> bool:
    """Count one call against `limit`, or return False when it is full.

    Check and increment are one statement: the upsert's `WHERE` is evaluated
    against the row as it is once this transaction holds its lock, so a
    concurrent `take` that committed the last slot first is seen here.
    """
    if limit.max <= 0:
        return False
    table = m.ToolUsageCounter.__table__
    stmt = (
        pg_insert(m.ToolUsageCounter)
        .values(
            id=uuid7(),
            tenant_id=tenant_id,
            agent_id=agent_id,
            tool_key=tool_key,
            tools_key=limit.key,
            period=limit.per,
            window_start=window_start(limit.per, now),
            count=1,
        )
        .on_conflict_do_update(
            constraint="uq_tool_usage_counter_window",
            set_={"count": table.c.count + 1, "updated_at": dt.datetime.now(dt.UTC)},
            where=table.c.count < limit.max,
        )
        .returning(m.ToolUsageCounter.count)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def give_back(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_key: str,
    limit: UsageLimit,
    now: dt.datetime,
) -> None:
    """Undo one `take` taken at `now` -- the call never reached the tool."""
    await db.execute(
        update(m.ToolUsageCounter)
        .where(
            m.ToolUsageCounter.tenant_id == tenant_id,
            m.ToolUsageCounter.agent_id == agent_id,
            m.ToolUsageCounter.tool_key == tool_key,
            m.ToolUsageCounter.tools_key == limit.key,
            m.ToolUsageCounter.period == limit.per,
            m.ToolUsageCounter.window_start == window_start(limit.per, now),
            m.ToolUsageCounter.count > 0,
        )
        .values(count=m.ToolUsageCounter.count - 1, updated_at=dt.datetime.now(dt.UTC))
    )


async def used(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_key: str,
    limit: UsageLimit,
    now: dt.datetime,
) -> int:
    """How many calls `limit`'s current window has counted. Read-only."""
    count = (
        await db.execute(
            select(m.ToolUsageCounter.count).where(
                m.ToolUsageCounter.tenant_id == tenant_id,
                m.ToolUsageCounter.agent_id == agent_id,
                m.ToolUsageCounter.tool_key == tool_key,
                m.ToolUsageCounter.tools_key == limit.key,
                m.ToolUsageCounter.period == limit.per,
                m.ToolUsageCounter.window_start == window_start(limit.per, now),
            )
        )
    ).scalar_one_or_none()
    return int(count or 0)


@dataclass(frozen=True)
class Usage:
    """One limit's count in the window `now` falls in -- the admin's "today"."""

    tool_key: str
    tools_key: str
    per: str
    used: int
    resets_at: dt.datetime


async def current_usage(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> list[Usage]:
    """Every count this agent has in its current windows, sorted."""
    now = now or dt.datetime.now(dt.UTC)
    current = {per: window_start(per, now) for per in _PERIOD_LENGTH}
    rows = (
        await db.execute(
            select(m.ToolUsageCounter).where(
                m.ToolUsageCounter.tenant_id == tenant_id,
                m.ToolUsageCounter.agent_id == agent_id,
                m.ToolUsageCounter.window_start >= min(current.values()),
            )
        )
    ).scalars()
    out = [
        Usage(
            tool_key=row.tool_key,
            tools_key=row.tools_key,
            per=row.period,
            used=row.count,
            resets_at=window_end(row.period, row.window_start),
        )
        for row in rows
        if row.period in current and row.window_start == current[row.period]
    ]
    return sorted(out, key=lambda u: (u.tool_key, u.tools_key, u.per))


async def prune_usage_counters(
    db: AsyncSession, *, tenant_id: uuid.UUID, now: dt.datetime | None = None
) -> int:
    """Delete this tenant's windows older than `KEEP_WINDOWS_FOR`."""
    now = now or dt.datetime.now(dt.UTC)
    result = await db.execute(
        delete(m.ToolUsageCounter).where(
            m.ToolUsageCounter.tenant_id == tenant_id,
            m.ToolUsageCounter.window_start < window_start("day", now) - KEEP_WINDOWS_FOR,
        )
    )
    # CursorResult carries rowcount; the base Result type does not.
    return int(getattr(result, "rowcount", 0) or 0)


async def prune_all_usage_counters() -> int:
    """One housekeeping tick: prune old windows in every tenant; never raises.
    Each tenant is its own transaction -- the tenant binding is transaction-
    local, the same reason `records.registry.prune_expired_links` works so."""
    from oc8.db.session import tenant_session
    from oc8.triggers.scheduler import list_active_tenant_ids

    deleted = 0
    try:
        tenants = await list_active_tenant_ids()
    except Exception:
        logger.warning("usage limits: could not list tenants; skipping prune", exc_info=True)
        return 0
    for tenant_id in tenants:
        try:
            async with tenant_session(tenant_id) as db:
                deleted += await prune_usage_counters(db, tenant_id=tenant_id)
        except Exception:
            logger.warning("usage limits: prune failed for tenant %s", tenant_id, exc_info=True)
    if deleted:
        logger.info("usage limits: pruned %d old window(s)", deleted)
    return deleted
