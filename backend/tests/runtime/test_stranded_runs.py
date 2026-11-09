"""A run committed as queued whose stream entry never landed must still run.

Every run source commits the row first and publishes it second (the worker can
load the run the instant the entry exists, so it must be durable first). The
gap between the two is real: the process dies, Redis is down, the publish
raises. The row then sits in `queued` for ever -- the queue's reclaim only sees
entries that exist, and the abandoned-run sweep only looks at `running` -- and
the UI shows the work as waiting to start, indefinitely.

`recover_stranded_runs` re-publishes such runs. The dangerous half is not the
stranded run, it is the storm: republishing work that is legitimately waiting,
or republishing the same run on every tick. Most tests here are about what must
be LEFT ALONE.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, update

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage, RunQueue
from oc8.runtime.reconcile import recover_stranded_runs
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

# The sweep is global by design -- it discovers every tenant, exactly as the
# abandoned-run sweep does -- so every assertion is about this test's own run.

pytestmark = pytest.mark.asyncio

STRANDED_FOR = dt.timedelta(minutes=30)


def _isolated_queue(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> RunQueue:
    queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.reconcile.get_run_queue", lambda: queue)
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    return queue


async def _drain(queue: RunQueue) -> list[RunMessage]:
    messages: list[RunMessage] = []
    while (msg := await queue.dequeue(timeout=0.5)) is not None:
        messages.append(msg)
    return messages


def _mine(messages: list[RunMessage], run_id: uuid.UUID) -> list[RunMessage]:
    return [msg for msg in messages if msg["run_id"] == str(run_id)]


async def _agent(db: Any, tenant: uuid.UUID, *, status: str = "idle") -> m.Agent:
    if await db.get(m.Organization, tenant) is None:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
        await db.flush()
    dept = m.Department(tenant_id=tenant, name="Kundenservice", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Sina",
        status=status,
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    return agent


async def _run(
    db: Any,
    tenant: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    state: str = "queued",
    age: dt.timedelta = STRANDED_FOR,
) -> uuid.UUID:
    run = m.AgentRun(tenant_id=tenant, agent_id=agent_id, state=state, context={"task": "x"})
    db.add(run)
    await db.flush()
    # Explicit value, or the column's onupdate would stamp now() over it.
    await db.execute(
        update(m.AgentRun)
        .where(m.AgentRun.id == run.id)
        .values(updated_at=dt.datetime.now(tz=dt.UTC) - age)
    )
    await db.commit()
    return run.id


class _CountingRuntime:
    """Counts how often the runtime is actually entered -- the one number that
    says whether a run executed once."""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, db: Any, **kw: Any) -> RunResult:
        self.calls += 1
        # Long enough for a second, concurrent delivery to get all the way to
        # the point where it would start the run too.
        await asyncio.sleep(0.3)
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="finished",
            tool_calls=[],
            steps=1,
        )


async def test_a_stranded_queued_run_is_republished_and_executes_once(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        run_id = await _run(db, tenant, agent.id)

    assert run_id in await recover_stranded_runs()

    mine = _mine(await _drain(queue), run_id)
    assert len(mine) == 1
    runtime = _CountingRuntime()
    for msg in mine:
        await execute_run(msg, runtime=runtime)
    assert runtime.calls == 1

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.DONE.value


async def test_a_young_queued_run_is_left_to_its_own_publish(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the threshold a run is simply waiting for a worker."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        run_id = await _run(db, tenant, agent.id, age=dt.timedelta(seconds=5))

    assert run_id not in await recover_stranded_runs()
    assert _mine(await _drain(queue), run_id) == []


async def test_a_running_run_is_not_touched(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, status="running")
        run_id = await _run(db, tenant, agent.id, state="running")

    assert run_id not in await recover_stranded_runs()
    assert _mine(await _drain(queue), run_id) == []
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.RUNNING.value


async def test_a_published_run_still_waiting_in_the_stream_is_not_republished(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old is not stranded when the entry is right there: under load a run can
    wait in the stream longer than the threshold, and republishing every run of
    a backlog would double the backlog."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        run_id = await _run(db, tenant, agent.id)
    await queue.enqueue(run_id=run_id, tenant_id=tenant)

    assert run_id not in await recover_stranded_runs()
    assert len(_mine(await _drain(queue), run_id)) == 1


async def test_a_double_publish_executes_once(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep cannot know an entry is not in flight somewhere; it relies on
    the worker refusing a run somebody else already started. Two deliveries of
    one run, processed at the same moment by two workers."""
    _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        run_id = await _run(db, tenant, agent.id, age=dt.timedelta(0))

    runtime = _CountingRuntime()
    first = RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="1-0", redelivered=False)
    second = RunMessage(
        run_id=str(run_id), tenant_id=str(tenant), entry_id="2-0", redelivered=False
    )
    await asyncio.gather(execute_run(first, runtime=runtime), execute_run(second, runtime=runtime))
    assert runtime.calls == 1

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.DONE.value
        hops = (
            (
                await db.execute(
                    select(m.RunStateTransition.to_state).where(
                        m.RunStateTransition.run_id == run_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(hops).count(RunState.RUNNING.value) == 1


async def test_concurrent_sweeps_republish_a_run_once(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        run_id = await _run(db, tenant, agent.id)

    results = await asyncio.gather(*(recover_stranded_runs() for _ in range(4)))
    assert sum(run_id in r for r in results) == 1
    assert len(_mine(await _drain(queue), run_id)) == 1


async def test_a_run_another_sweep_holds_is_skipped_not_waited_for(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SKIP LOCKED: a row somebody else is already republishing needs nothing
    from us, and waiting on it would stall the sweep behind them."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        run_id = await _run(db, tenant, agent.id)

    async with app_session(tenant) as holder:
        await holder.execute(select(m.AgentRun.id).where(m.AgentRun.id == run_id).with_for_update())
        swept = await asyncio.wait_for(recover_stranded_runs(), timeout=20.0)
        assert run_id not in swept

    assert _mine(await _drain(queue), run_id) == []
    assert run_id in await recover_stranded_runs()


async def test_a_republished_run_is_not_republished_on_the_next_tick(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker: one republish per threshold window, not one per tick."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        run_id = await _run(db, tenant, agent.id)

    assert run_id in await recover_stranded_runs()
    # Delivered (so not in the stream's backlog any more) but never acted on --
    # the case where only the marker stands between the run and a storm.
    assert len(_mine(await _drain(queue), run_id)) == 1
    for _ in range(3):
        assert run_id not in await recover_stranded_runs()
    assert _mine(await _drain(queue), run_id) == []


@pytest.mark.parametrize("status", ["paused", "stopped", "pending_approval"])
async def test_a_run_of_a_held_agent_is_not_republished(
    app_session: AppSessionFactory,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """Work deliberately on hold stays on hold; it is picked up once the hold
    ends, because it is then an ordinary stranded run."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, status=status)
        agent_id = agent.id
        run_id = await _run(db, tenant, agent.id)

    for _ in range(3):
        assert run_id not in await recover_stranded_runs()
    assert _mine(await _drain(queue), run_id) == []

    async with app_session(tenant) as db:
        await db.execute(update(m.Agent).where(m.Agent.id == agent_id).values(status="idle"))
    assert run_id in await recover_stranded_runs()


async def test_a_run_waiting_behind_its_agents_running_run_is_not_republished(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run per agent: the worker defers this run and its entry comes back
    on its own. Republishing it would only add an entry that defers too -- and
    every deferred entry keeps itself pending, so they would accumulate for as
    long as the agent is busy."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, status="running")
        await _run(db, tenant, agent.id, state="running", age=dt.timedelta(0))
    # `_run` commits, and the tenant binding ends with the transaction.
    async with app_session(tenant) as db:
        run_id = await _run(db, tenant, agent.id)

    for _ in range(3):
        assert run_id not in await recover_stranded_runs()
    assert _mine(await _drain(queue), run_id) == []
