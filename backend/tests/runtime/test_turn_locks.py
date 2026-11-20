"""What a running turn keeps locked while its model is thinking.

A Copilot or agent turn on the in-process runtime is ONE transaction from the
executor's claim to its terminal commit. A slow model call therefore holds every
lock the transaction took before it -- in a browser E2E with a slow local model
one turn held the Copilot's agent row for over 40 minutes, and the builder
card, model edits and Stop all waited on it.

The model call here is a runtime that blocks on an event; each concurrent action
gets its own session with a short `lock_timeout`, so a wait shows up as
`LockNotAvailable` rather than as a hung test.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.auth import Principal
from oc8.copilot.builder import apply_builder_proposal
from oc8.runtime.cancellation import record_operator_cancel
from oc8.runtime.executor import execute_run
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import builder_office, propose_as_admin

pytestmark = pytest.mark.asyncio

#: How long a concurrent action may wait on a lock before it counts as blocked.
LOCK_BUDGET = "2s"


class _ThinkingRuntime:
    """A runtime whose model call does not return until released."""

    def __init__(self) -> None:
        self.thinking = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, db: AsyncSession, **kw: Any) -> RunResult:
        self.thinking.set()
        await asyncio.wait_for(self.release.wait(), timeout=60)
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="finished",
            tool_calls=[],
            steps=1,
        )


async def _bounded(db: AsyncSession) -> None:
    await db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_BUDGET}'"))


async def test_a_thinking_turn_blocks_nobody(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
        await db.flush()
        office = await builder_office(db, tenant)
        card = await propose_as_admin(db, office)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=office.assistant_id,
            state=RunState.QUEUED.value,
            source="chat",
            context={"task": "hallo"},
        )
        db.add(run)
        await db.flush()
    run_id, card_id = run.id, card.id

    runtime = _ThinkingRuntime()
    turn = asyncio.create_task(execute_run(_message(run_id, tenant), runtime=runtime))
    await asyncio.wait_for(runtime.thinking.wait(), timeout=10)

    async def switch_model() -> None:
        async with app_session(tenant) as db:
            await _bounded(db)
            agent = await db.get(m.Agent, office.assistant_id)
            assert agent is not None
            agent.model_config_id = office.model_id
            agent.definition = {**(agent.definition or {}), "max_steps": 7}
            await db.flush()

    async def stop() -> None:
        async with app_session(tenant) as db:
            await _bounded(db)
            row = await db.get(m.AgentRun, run_id)
            assert row is not None
            await record_operator_cancel(db, run=row, principal=None, actor_id=None)

    async def apply_card() -> None:
        async with app_session(tenant) as db:
            await _bounded(db)
            await apply_builder_proposal(
                db,
                proposal_id=card_id,
                expect_status="draft",
                applied_by="admin",
                principal=Principal(subject="admin", tenant_id=tenant, role="org_admin"),
                now=dt.datetime.now(tz=dt.UTC),
            )

    blocked: dict[str, str] = {}
    try:
        for name, action in (("model switch", switch_model), ("stop", stop), ("apply", apply_card)):
            try:
                await asyncio.wait_for(action(), timeout=10)
            except Exception as exc:
                blocked[name] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
    finally:
        runtime.release.set()
        await asyncio.wait_for(turn, timeout=30)

    assert blocked == {}
    async with app_session(tenant) as db:
        finished = await db.get(m.AgentRun, run_id)
        assert finished is not None
        assert finished.state in {RunState.DONE.value, RunState.INTERRUPTED.value}
        agent = await db.get(m.Agent, office.assistant_id)
        assert agent is not None
        # The switch made mid-turn survives the turn's own ending.
        assert agent.model_config_id == office.model_id
        assert agent.status == "idle"


async def _office_with_queued_runs(
    app_session: AppSessionFactory, tenant: uuid.UUID, count: int
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
        await db.flush()
        office = await builder_office(db, tenant)
        runs = [
            m.AgentRun(
                tenant_id=tenant,
                agent_id=office.assistant_id,
                state=RunState.QUEUED.value,
                context={"task": f"turn {i}"},
            )
            for i in range(count)
        ]
        db.add_all(runs)
        await db.flush()
    return office.assistant_id, [run.id for run in runs]


def _message(run_id: uuid.UUID, tenant: uuid.UUID) -> Any:
    return {"run_id": str(run_id), "tenant_id": str(tenant)}


async def test_a_pause_made_during_the_turn_is_not_undone_by_its_end(
    app_session: AppSessionFactory,
) -> None:
    """The row is free mid-run now, so a pause lands at once -- and the run's
    ending must not write "idle" over it (it used to wait, then win)."""
    tenant = uuid.uuid4()
    agent_id, (run_id,) = await _office_with_queued_runs(app_session, tenant, 1)
    runtime = _ThinkingRuntime()
    turn = asyncio.create_task(execute_run(_message(run_id, tenant), runtime=runtime))
    await asyncio.wait_for(runtime.thinking.wait(), timeout=10)
    try:
        async with app_session(tenant) as db:
            await _bounded(db)
            agent = await db.get(m.Agent, agent_id)
            assert agent is not None
            assert agent.status == "running"  # committed, not hidden in the run
            agent.status = "paused"
    finally:
        runtime.release.set()
        await asyncio.wait_for(turn, timeout=30)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert agent.status == "paused"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.DONE.value


class _CountingThinkingRuntime(_ThinkingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, db: AsyncSession, **kw: Any) -> RunResult:
        self.calls += 1
        if self.calls > 1:
            return RunResult(uuid.uuid4(), kw["agent"].id, "done", "second", [], 1)
        return await super().execute(db, **kw)


async def test_one_run_per_agent_and_one_start_per_run_still_hold(
    app_session: AppSessionFactory,
) -> None:
    """The run slot replaces the agent row lock: a second delivery of the
    thinking run never starts it again, and the agent's next run waits for the
    first to end instead of running beside it."""
    tenant = uuid.uuid4()
    _agent_id, (first, second) = await _office_with_queued_runs(app_session, tenant, 2)
    runtime = _CountingThinkingRuntime()
    turn = asyncio.create_task(execute_run(_message(first, tenant), runtime=runtime))
    await asyncio.wait_for(runtime.thinking.wait(), timeout=10)
    again = asyncio.create_task(execute_run(_message(first, tenant), runtime=runtime))
    other = asyncio.create_task(execute_run(_message(second, tenant), runtime=runtime))
    await asyncio.sleep(1.0)
    assert runtime.calls == 1
    assert not again.done() and not other.done()
    runtime.release.set()
    await asyncio.wait_for(asyncio.gather(turn, again, other), timeout=30)
    assert runtime.calls == 2
    async with app_session(tenant) as db:
        for run_id in (first, second):
            run = await db.get(m.AgentRun, run_id)
            assert run is not None and run.state == RunState.DONE.value
