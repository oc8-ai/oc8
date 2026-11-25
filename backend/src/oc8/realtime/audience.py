"""Who may receive what an agent is doing, live (spec A4, ruling R5).

The feed socket hands an every-agent run reader every envelope of the tenant
that names no audience (`realtime.manager._route`). What a personal agent
produces -- streamed text and reasoning, tool calls, step timings, container
output, rendered components, todos, activity lines -- is private to its
department's owner, so it goes out addressed to that owner alone, and to nobody
when the owner has left (R1 fails closed). Its `run.status` / `agent.status`
stay tenant-wide: metadata (R2).

The owner is resolved once per agent and kept in a process-local LRU keyed by
`agent_id`: a personal agent cannot move departments and a department's kind
and owner are set at creation, so the answer is stable for the agent's
lifetime -- except that the owner can leave. `forget_owner` (called once an
offboarding commits) drops what this process cached for them; every other
process re-reads an owner within `OWNER_TTL_SECONDS`, and the departed
member's sockets are closed everywhere (`EventBus.publish_member_gone`).
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.models.core import DEPARTMENT_KIND_PERSONAL

#: How many agents' answers one process keeps (R5).
_CACHE_MAXSIZE = 4096


@dataclass(frozen=True)
class PersonalAudience:
    """`personal` False: a standard (or assistant) agent, published tenant-wide.
    `personal` True: only `owner_member_id` receives it -- nobody when None."""

    personal: bool
    owner_member_id: uuid.UUID | None = None


STANDARD = PersonalAudience(personal=False)
#: An agent the lookup could not see: treated like a personal agent without an
#: owner, so content goes to nobody rather than to everybody.
UNKNOWN = PersonalAudience(personal=True)

#: How long an answer naming an owner is trusted. The owner can leave, and
#: `forget_owner` only reaches the process that committed the offboarding: every
#: other process (workers, runtimes, other replicas) re-reads within this window.
OWNER_TTL_SECONDS = 60.0
#: How long "the agent cannot be seen" is trusted, so a misconfigured caller does
#: not cost one database round-trip per streamed token.
UNKNOWN_TTL_SECONDS = 5.0

#: The cache's clock; tests replace it.
_clock: Callable[[], float] = time.monotonic

#: Bumped by `forget_owner`. A lookup that started under an older generation may
#: have read an owner who has since left, so its answer is never cached.
_generation = 0


@dataclass(frozen=True)
class _Entry:
    answer: PersonalAudience
    #: On `_clock`; None never expires (a standard agent, a departed owner).
    expires_at: float | None


_cache: OrderedDict[uuid.UUID, _Entry] = OrderedDict()


def _expiry(answer: PersonalAudience) -> float | None:
    if answer is UNKNOWN:
        return _clock() + UNKNOWN_TTL_SECONDS
    if answer.owner_member_id is not None:
        return _clock() + OWNER_TTL_SECONDS
    return None


async def _lookup(db: AsyncSession, agent_id: uuid.UUID) -> PersonalAudience | None:
    """The uncached answer, or None when the agent cannot be seen. Archived
    agents and departments still answer: their last events are content too."""
    from oc8.departments.personal import live_department_owner

    kind = (
        await db.execute(
            select(m.Department.kind)
            .join(m.Agent, m.Agent.department_id == m.Department.id)
            .where(m.Agent.id == agent_id, m.Department.tenant_id == m.Agent.tenant_id)
        )
    ).first()
    if kind is None:
        return None
    if kind[0] != DEPARTMENT_KIND_PERSONAL:
        return STANDARD
    return PersonalAudience(
        personal=True, owner_member_id=await live_department_owner(db, agent_id)
    )


def cached(agent_id: uuid.UUID) -> PersonalAudience | None:
    """The cached answer for `agent_id`, without touching the database; None
    when there is none or it has expired."""
    entry = _cache.get(agent_id)
    if entry is None:
        return None
    if entry.expires_at is not None and _clock() >= entry.expires_at:
        _cache.pop(agent_id, None)
        return None
    _cache.move_to_end(agent_id)
    return entry.answer


async def personal_audience(db: AsyncSession, agent_id: uuid.UUID) -> PersonalAudience:
    """Whether `agent_id` is a personal agent, and whose sockets its content
    may reach. `db` must be bound to the agent's tenant (RLS): an unbound
    session sees no agent and answers `UNKNOWN`, which publishes nothing.

    A lookup that straddles a `forget_owner` is read once more (the departure
    has committed by then) and, if the generation moved again, answered
    without being cached."""
    hit = cached(agent_id)
    if hit is not None:
        return hit
    answer = UNKNOWN
    for _ in range(2):
        started = _generation
        answer = await _lookup(db, agent_id) or UNKNOWN
        if started == _generation:
            _cache[agent_id] = _Entry(answer, _expiry(answer))
            if len(_cache) > _CACHE_MAXSIZE:
                _cache.popitem(last=False)
            return answer
    return answer


def forget_owner(member_id: uuid.UUID) -> None:
    """Drop every cached answer naming `member_id` as owner: they have left,
    and the next lookup finds their department without one. Bumps the
    generation, so a lookup already in flight cannot cache them again."""
    global _generation
    _generation += 1
    for agent_id in [a for a, e in _cache.items() if e.answer.owner_member_id == member_id]:
        _cache.pop(agent_id, None)


def clear_cache() -> None:
    _cache.clear()
