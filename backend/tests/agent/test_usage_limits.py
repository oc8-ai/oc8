"""Usage limits are counted by the core: "at most 50 mails a day".

The count is taken atomically BEFORE the call (check-and-increment in one
statement), so two parallel runs at 49/50 cannot both send the 50th mail, and
given back when the call did not succeed, so only successful calls count.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.harness.stages.b_usage_limit import UsageHold, reserve_usage
from oc8.agent.usage_limits import (
    Usage,
    current_usage,
    limit_refusal,
    prune_usage_counters,
    take,
    window_start,
)
from oc8.authz.pdp import ToolPolicy, UsageLimit

SENDING = UsageLimit(tools=("mail_send", "mail_reply", "mail_forward"), max=50, per="day")
NOON = dt.datetime(2026, 11, 22, 12, 30, tzinfo=dt.UTC)


def _policy(*limits: UsageLimit) -> ToolPolicy:
    return ToolPolicy(enabled=True, read=True, modify=True, limits=limits)


async def _take(
    db: AsyncSession,
    tenant: uuid.UUID,
    agent: uuid.UUID,
    limit: UsageLimit,
    *,
    tool_key: str = "email",
    now: dt.datetime = NOON,
) -> bool:
    return await take(db, tenant_id=tenant, agent_id=agent, tool_key=tool_key, limit=limit, now=now)


async def _reserve(tenant: uuid.UUID, agent: uuid.UUID, tool: str, policy: ToolPolicy) -> UsageHold:
    return await reserve_usage(
        tenant_id=tenant, agent_id=agent, tool_key="email", tool=tool, policy=policy, now=NOON
    )


async def _count(db: AsyncSession, tenant: uuid.UUID, agent: uuid.UUID) -> int:
    rows = (
        await db.execute(
            select(m.ToolUsageCounter.count).where(
                m.ToolUsageCounter.tenant_id == tenant, m.ToolUsageCounter.agent_id == agent
            )
        )
    ).scalars()
    return sum(rows)


async def _usage(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent: uuid.UUID
) -> list[Usage]:
    async with app_session(tenant) as db:
        return await current_usage(db, tenant_id=tenant, agent_id=agent, now=NOON)


# ------------------------------------------------------------------ the window


def test_a_day_is_a_utc_calendar_day_and_an_hour_a_clock_hour() -> None:
    late = dt.datetime(2026, 11, 22, 23, 59, tzinfo=dt.timezone(dt.timedelta(hours=-2)))
    assert window_start("day", late) == dt.datetime(2026, 11, 23, tzinfo=dt.UTC)
    assert window_start("hour", NOON) == dt.datetime(2026, 11, 22, 12, tzinfo=dt.UTC)


def test_the_refusal_says_which_limit_and_when_it_resets() -> None:
    assert limit_refusal(SENDING, NOON) == (
        "ERROR: Daily limit of 50 reached for mail_send/mail_reply/mail_forward; "
        "resets at 00:00 UTC. This is a policy decision, not a tool error -- do "
        "not retry it now or work around it with another tool."
    )
    hourly = UsageLimit(tools=("mail_send",), max=5, per="hour")
    assert "Hourly limit of 5 reached for mail_send; resets at 13:00 UTC." in limit_refusal(
        hourly, NOON
    )


# ------------------------------------------------------------------ the count


async def test_take_counts_up_to_max_and_then_refuses(app_session: AppSessionFactory) -> None:
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    small = UsageLimit(tools=("mail_send",), max=2)
    async with app_session(tenant) as db:
        assert await _take(db, tenant, agent, small)
        assert await _take(db, tenant, agent, small)
        assert not await _take(db, tenant, agent, small)
        assert await _count(db, tenant, agent) == 2


async def test_a_limit_of_zero_refuses_the_first_call(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        zero = UsageLimit(tools=("mail_send",), max=0)
        assert not await _take(db, tenant, uuid.uuid4(), zero)


async def test_the_next_day_starts_from_zero(app_session: AppSessionFactory) -> None:
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    one = UsageLimit(tools=("mail_send",), max=1)
    async with app_session(tenant) as db:
        assert await _take(db, tenant, agent, one)
        assert not await _take(db, tenant, agent, one)
        assert await _take(db, tenant, agent, one, now=NOON + dt.timedelta(days=1))


async def test_another_agent_or_tool_key_has_its_own_count(
    app_session: AppSessionFactory,
) -> None:
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    one = UsageLimit(tools=("mail_send",), max=1)
    async with app_session(tenant) as db:
        assert await _take(db, tenant, agent, one)
        assert await _take(db, tenant, uuid.uuid4(), one)
        assert await _take(db, tenant, agent, one, tool_key="email_b")


async def test_two_parallel_calls_at_the_edge_do_not_both_pass(
    app_session: AppSessionFactory,
) -> None:
    """49 of 50 used, two runs of the same agent send at once: exactly one of
    them gets the 50th mail. Each in its own transaction, as two runs are."""
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        for _ in range(49):
            assert await _take(db, tenant, agent, SENDING)

    start = asyncio.Event()

    async def one_run() -> bool:
        async with app_session(tenant) as db:
            await start.wait()
            taken = await _take(db, tenant, agent, SENDING)
            # Hold the transaction open a moment, as a slow run would.
            await asyncio.sleep(0.2)
            return taken

    runs = [asyncio.create_task(one_run()) for _ in range(2)]
    await asyncio.sleep(0.1)
    start.set()
    assert sorted(await asyncio.gather(*runs)) == [False, True]
    async with app_session(tenant) as db:
        assert await _count(db, tenant, agent) == 50


# ------------------------------------------------------------------ the hold


async def test_a_call_without_a_limit_touches_nothing() -> None:
    hold = await _reserve(uuid.uuid4(), uuid.uuid4(), "mail_get", _policy(SENDING))
    assert hold.refusal is None
    assert hold.taken == ()


async def test_a_successful_call_keeps_its_count_and_a_failed_one_gives_it_back(
    app_session: AppSessionFactory,
) -> None:
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    kept = await _reserve(tenant, agent, "mail_reply", _policy(SENDING))
    await kept.settle(succeeded=True)
    failed = await _reserve(tenant, agent, "mail_reply", _policy(SENDING))
    await failed.settle(succeeded=False)
    async with app_session(tenant) as db:
        assert await _count(db, tenant, agent) == 1


async def test_reserve_usage_refuses_at_the_limit_and_takes_nothing_from_the_others(
    app_session: AppSessionFactory,
) -> None:
    """Two limits on one tool: when one is full the call is refused, and the
    count the other limit had already given is handed back."""
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    hourly = UsageLimit(tools=("mail_send",), max=1, per="hour")
    first = await _reserve(tenant, agent, "mail_send", _policy(SENDING, hourly))
    assert first.refusal is None
    await first.settle(succeeded=True)
    second = await _reserve(tenant, agent, "mail_send", _policy(SENDING, hourly))
    assert second.refusal is not None
    assert "Hourly limit of 1 reached for mail_send" in second.refusal
    usage = {(u.tools_key, u.per): u.used for u in await _usage(app_session, tenant, agent)}
    assert usage == {("mail_forward/mail_reply/mail_send", "day"): 1, ("mail_send", "hour"): 1}


async def test_two_parallel_reservations_at_the_edge_do_not_both_pass() -> None:
    """The same race through the hold the runtimes use: its own short
    transaction per reservation, so a run's open transaction never holds the
    counter's row lock while it waits for the model."""
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    policy = _policy(UsageLimit(tools=("mail_send",), max=1))
    holds = await asyncio.gather(*(_reserve(tenant, agent, "mail_send", policy) for _ in range(5)))
    assert sorted(h.refusal is None for h in holds) == [False] * 4 + [True]


async def test_current_usage_lists_this_windows_counts_only(
    app_session: AppSessionFactory,
) -> None:
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        await _take(db, tenant, agent, SENDING, now=NOON - dt.timedelta(days=1))
        await _take(db, tenant, agent, SENDING)
        await _take(db, tenant, agent, SENDING)
    usage = await _usage(app_session, tenant, agent)
    assert [(u.tool_key, u.tools_key, u.per, u.used) for u in usage] == [
        ("email", "mail_forward/mail_reply/mail_send", "day", 2)
    ]
    assert usage[0].resets_at == dt.datetime(2026, 11, 23, tzinfo=dt.UTC)


async def test_old_windows_are_pruned(app_session: AppSessionFactory) -> None:
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        await _take(db, tenant, agent, SENDING, now=NOON - dt.timedelta(days=3))
        await _take(db, tenant, agent, SENDING)
        assert await prune_usage_counters(db, tenant_id=tenant, now=NOON) == 1
        assert await _count(db, tenant, agent) == 1
