"""The agent's status while nobody holds its row for a run.

The executor no longer locks the agent row for an in-process run
(`oc8.runtime.agent_status`), so a pause, a stop or a budget hard-stop can land
at any moment of a run -- between the claim and "running", during the model
call, during an early exit. Each test makes one of those races happen on
purpose and checks the run leaves the other writer's status alone, and that a
run which never commits does not leave its agent "running".
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.runtime import executor
from oc8.runtime.executor import execute_run
from oc8.runtime.reconcile import STALE_RUNNING_AFTER, release_stale_running_agents
from oc8.runtime.states import RunState
from oc8.tenants.activity import tenant_may_run
from tests.conftest import AppSessionFactory
from tests.runtime.test_turn_locks import _message, _office_with_queued_runs, _ThinkingRuntime

pytestmark = pytest.mark.asyncio


async def _set_agent(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID, **values: Any
) -> None:
    async with app_session(tenant) as db:
        await db.execute(update(m.Agent).where(m.Agent.id == agent_id).values(**values))


async def _agent(app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID) -> m.Agent:
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        return agent


async def _run_state(app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID) -> str:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        return run.state


async def test_a_pause_racing_the_running_commit_is_kept(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paused after the claim read the agent, before "running" was written."""
    tenant = uuid.uuid4()
    agent_id, (run_id,) = await _office_with_queued_runs(app_session, tenant, 1)
    real_gate = tenant_may_run
    paused = False

    async def gate(tid: uuid.UUID) -> bool:
        nonlocal paused
        if not paused:
            paused = True
            await _set_agent(app_session, tenant, agent_id, status="paused")
        return await real_gate(tid)

    monkeypatch.setattr(executor, "tenant_may_run", gate)
    runtime = _ThinkingRuntime()
    turn = asyncio.create_task(execute_run(_message(run_id, tenant), runtime=runtime))
    try:
        await asyncio.wait_for(runtime.thinking.wait(), timeout=10)
        assert (await _agent(app_session, tenant, agent_id)).status == "paused"
    finally:
        runtime.release.set()
        await asyncio.wait_for(turn, timeout=30)
    assert (await _agent(app_session, tenant, agent_id)).status == "paused"
    assert await _run_state(app_session, tenant, run_id) == RunState.DONE.value


@pytest.mark.parametrize("exit_path", ["cancel_drain", "organisation_inactive"])
async def test_an_early_exit_racing_a_budget_hard_stop_keeps_the_agent_frozen(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, exit_path: str
) -> None:
    """The hard-stop pauses the agent after the claim read it; the exit's
    "free the agent unless paused" must see that pause, not its stale copy."""
    tenant = uuid.uuid4()
    agent_id, (run_id,) = await _office_with_queued_runs(app_session, tenant, 1)

    async def hard_stop() -> None:
        await _set_agent(
            app_session,
            tenant,
            agent_id,
            status="paused",
            pause_reason="budget",
            paused_at=dt.datetime.now(tz=dt.UTC),
        )

    if exit_path == "cancel_drain":
        async with app_session(tenant) as db:
            db.add(
                m.RunCancellation(
                    tenant_id=tenant,
                    run_id=run_id,
                    requested_at=dt.datetime.now(tz=dt.UTC),
                    cancellation_kind="operator_interrupted",
                )
            )
        real_kind = executor._cancellation_kind

        async def kind(db: AsyncSession, rid: uuid.UUID) -> str | None:
            await hard_stop()
            return await real_kind(db, rid)

        monkeypatch.setattr(executor, "_cancellation_kind", kind)
    else:

        async def gate(tid: uuid.UUID) -> bool:
            await hard_stop()
            return False

        monkeypatch.setattr(executor, "tenant_may_run", gate)

    await asyncio.wait_for(execute_run(_message(run_id, tenant)), timeout=30)

    agent = await _agent(app_session, tenant, agent_id)
    assert (agent.status, agent.pause_reason) == ("paused", "budget")
    assert await _run_state(app_session, tenant, run_id) == RunState.INTERRUPTED.value


class _RereadingRuntime:
    """Mid-run: somebody pauses the agent, then the run re-reads its row with
    `populate_existing` (as `visible_agent` in a control tool does)."""

    def __init__(self, app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
        self.app_session = app_session
        self.tenant = tenant

    async def execute(self, db: AsyncSession, **kw: Any) -> RunResult:
        agent_id = kw["agent"].id
        await _set_agent(self.app_session, self.tenant, agent_id, status="paused")
        await db.execute(
            select(m.Agent).where(m.Agent.id == agent_id).execution_options(populate_existing=True)
        )
        return RunResult(uuid.uuid4(), agent_id, "done", "finished", [], 1)


async def test_a_mid_run_reread_does_not_defeat_the_compare_and_set(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, (run_id,) = await _office_with_queued_runs(app_session, tenant, 1)
    await asyncio.wait_for(
        execute_run(_message(run_id, tenant), runtime=_RereadingRuntime(app_session, tenant)),
        timeout=30,
    )
    assert (await _agent(app_session, tenant, agent_id)).status == "paused"
    assert await _run_state(app_session, tenant, run_id) == RunState.DONE.value


async def test_a_failure_before_the_runtime_does_not_leave_the_agent_running(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "running" is committed on its own; the run's transaction then rolls back."""
    tenant = uuid.uuid4()
    agent_id, (run_id,) = await _office_with_queued_runs(app_session, tenant, 1)
    # The tenant Assistant is created "running"; start from a status the run
    # would visibly change.
    await _set_agent(app_session, tenant, agent_id, status="idle")

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("pinned version unreadable")

    monkeypatch.setattr(executor, "resolve_version", broken)
    with pytest.raises(RuntimeError):
        await execute_run(_message(run_id, tenant), runtime=_ThinkingRuntime())

    assert (await _agent(app_session, tenant, agent_id)).status == "idle"
    assert await _run_state(app_session, tenant, run_id) == RunState.QUEUED.value


async def test_a_held_agent_row_does_not_hold_up_the_run(
    app_session: AppSessionFactory,
) -> None:
    """Marking "running" waits at most a short lock timeout, then goes on."""
    tenant = uuid.uuid4()
    agent_id, (run_id,) = await _office_with_queued_runs(app_session, tenant, 1)
    runtime = _ThinkingRuntime()
    async with app_session(tenant) as holder:
        await holder.execute(select(m.Agent.id).where(m.Agent.id == agent_id).with_for_update())
        turn = asyncio.create_task(execute_run(_message(run_id, tenant), runtime=runtime))
        try:
            await asyncio.wait_for(runtime.thinking.wait(), timeout=8)
        finally:
            await holder.rollback()
            runtime.release.set()
            await asyncio.wait_for(turn, timeout=30)
    assert (await _agent(app_session, tenant, agent_id)).status == "idle"
    assert await _run_state(app_session, tenant, run_id) == RunState.DONE.value


async def _stale_running(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID
) -> None:
    await _set_agent(
        app_session,
        tenant,
        agent_id,
        status="running",
        updated_at=dt.datetime.now(tz=dt.UTC) - STALE_RUNNING_AFTER * 2,
    )


async def test_the_reconciler_releases_an_agent_left_running(
    app_session: AppSessionFactory,
) -> None:
    """The process died after "running" committed and before the run did."""
    tenant = uuid.uuid4()
    agent_id, (run_id,) = await _office_with_queued_runs(app_session, tenant, 1)
    await _stale_running(app_session, tenant, agent_id)

    assert agent_id in await release_stale_running_agents()
    assert (await _agent(app_session, tenant, agent_id)).status == "idle"
    assert await _run_state(app_session, tenant, run_id) == RunState.QUEUED.value


async def test_the_reconciler_leaves_a_live_or_parked_run_alone(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, (live, parked) = await _office_with_queued_runs(app_session, tenant, 2)

    # A run parked on a question keeps its agent "running".
    async with app_session(tenant) as db:
        await db.execute(
            update(m.AgentRun)
            .where(m.AgentRun.id == parked)
            .values(state=RunState.WAITING_FOR_INPUT.value)
        )
    await _stale_running(app_session, tenant, agent_id)
    assert agent_id not in await release_stale_running_agents()
    async with app_session(tenant) as db:
        await db.execute(
            update(m.AgentRun).where(m.AgentRun.id == parked).values(state=RunState.FAILED.value)
        )

    # An in-process run: its RUNNING is not committed, only its run slot shows.
    runtime = _ThinkingRuntime()
    turn = asyncio.create_task(execute_run(_message(live, tenant), runtime=runtime))
    try:
        await asyncio.wait_for(runtime.thinking.wait(), timeout=10)
        await _stale_running(app_session, tenant, agent_id)
        assert agent_id not in await release_stale_running_agents()
        assert (await _agent(app_session, tenant, agent_id)).status == "running"
    finally:
        runtime.release.set()
        await asyncio.wait_for(turn, timeout=30)
    assert (await _agent(app_session, tenant, agent_id)).status == "idle"
