"""B -- usage limits ("at most 50 mails a day"), the call site every runtime
shares (the engine, the isolated runtime and the tool gateway).

Asked LAST before the call, after the PEP, the record guards and the outward
rule: a call any of those refuses must not use up the agent's budget. The
logic and its reasons live in `oc8.agent.usage_limits`.

The count is taken in its OWN short transaction, committed at once, never in
the run's: the run's transaction stays open across every later model call, and
a row lock held that long would make a second run of the same agent wait for
the first to finish before it could send anything at all. Committed here, a
count stays taken even if the run later fails -- right, because the mail went
out -- and `settle` gives it back only when the call never reached the tool.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.agent.usage_limits import give_back, limit_refusal, take, used
from oc8.audit import append_event
from oc8.authz.pdp import ToolPolicy, UsageLimit, usage_limits_for
from oc8.realtime.emit import record_activity


@dataclass(frozen=True)
class UsageHold:
    """The counts one call took, or the refusal when a limit was full."""

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    tool_key: str
    now: dt.datetime
    taken: tuple[UsageLimit, ...] = ()
    #: What the model is told instead of calling. None -> go ahead.
    refusal: str | None = None

    async def settle(self, *, succeeded: bool) -> None:
        """Keep the counts of a call that reached the tool; give them back
        only for one that provably never did (`succeeded=False`): refused or
        held by oc8, a replay, waiting for an answer, or a connection that
        could not be opened. A call that reached the tool keeps its count even
        if it failed, timed out or was cancelled -- the far system may have
        acted before it failed. (`succeeded` names the caller's question:
        did the call happen?)"""
        if succeeded or not self.taken:
            return
        await _give_back_all(self, self.taken)


async def _give_back_all(hold: UsageHold, limits: tuple[UsageLimit, ...]) -> None:
    from oc8.db.session import tenant_session

    async with tenant_session(hold.tenant_id) as db:
        for limit in limits:
            await give_back(
                db,
                tenant_id=hold.tenant_id,
                agent_id=hold.agent_id,
                tool_key=hold.tool_key,
                limit=limit,
                now=hold.now,
            )


async def reserve_usage(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_key: str | None,
    tool: str,
    policy: ToolPolicy | None,
    now: dt.datetime | None = None,
) -> UsageHold:
    """Take one count from every limit on `tool`, all or none.

    A call with no limit on it costs nothing: no transaction is opened."""
    now = now or dt.datetime.now(dt.UTC)
    hold = UsageHold(tenant_id=tenant_id, agent_id=agent_id, tool_key=tool_key or "", now=now)
    limits = usage_limits_for(policy, tool) if tool_key else ()
    if not limits:
        return hold
    from oc8.db.session import tenant_session

    taken: list[UsageLimit] = []
    full: UsageLimit | None = None
    async with tenant_session(tenant_id) as db:
        # The same order every time, so two calls under several limits never
        # wait on each other's rows crosswise.
        for limit in sorted(limits, key=lambda lim: (lim.key, lim.per, lim.max)):
            if await take(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                tool_key=tool_key or "",
                limit=limit,
                now=now,
            ):
                taken.append(limit)
            else:
                full = limit
                break
        if full is not None:
            # All or none: a refused call counts against no limit. Undone in
            # the same transaction, so nothing of it is ever visible.
            for limit in taken:
                await give_back(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    tool_key=tool_key or "",
                    limit=limit,
                    now=now,
                )
    if full is not None:
        return UsageHold(
            tenant_id=tenant_id,
            agent_id=agent_id,
            tool_key=tool_key or "",
            now=now,
            refusal=limit_refusal(full, now),
        )
    return UsageHold(
        tenant_id=tenant_id,
        agent_id=agent_id,
        tool_key=tool_key or "",
        now=now,
        taken=tuple(taken),
    )


async def usage_full(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_key: str | None,
    tool: str,
    policy: ToolPolicy | None,
    now: dt.datetime | None = None,
) -> str | None:
    """Read-only: the refusal a call would get if it ran now, or None.

    Asked before a call is parked for approval: a person should not be asked
    to approve a call the limit would refuse anyway. Takes nothing -- the call
    is counted (and may still be refused) when it actually runs."""
    now = now or dt.datetime.now(dt.UTC)
    limits = usage_limits_for(policy, tool) if tool_key else ()
    if not limits:
        return None
    from oc8.db.session import tenant_session

    async with tenant_session(tenant_id) as db:
        for limit in sorted(limits, key=lambda lim: (lim.key, lim.per, lim.max)):
            if (
                await used(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    tool_key=tool_key or "",
                    limit=limit,
                    now=now,
                )
                >= limit.max
            ):
                return limit_refusal(limit, now)
    return None


def usage_limit_reason(refusal: str) -> str:
    """The refusal as a policy reason: no "ERROR:" prefix, no advice to the model."""
    reason = refusal.removeprefix("ERROR: ").strip()
    return reason.split(" This is a policy decision", 1)[0]


async def record_limit_refusal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool: str,
    arguments: dict[str, Any] | None,
    refusal: str,
    originating_operator: str | None = None,
) -> None:
    """Audit and show a refusal like the other guardrail refusals: a `deny`
    in the audit trail and a warning in the agent's activity feed, so a
    limit that is hit is seen by a person, not only by the model."""
    reason = refusal.removeprefix("ERROR: ").split(" This is a policy decision")[0]
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="agent",
        actor_id=agent_id,
        category="tool_action",
        action=f"tool.call:{tool}",
        resource={"tool": tool, "arguments": arguments or {}},
        decision="deny",
        reason=reason,
        originating_operator=originating_operator,
    )
    await record_activity(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        status="warning",
        message=reason,
    )
