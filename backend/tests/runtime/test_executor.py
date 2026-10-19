from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.channels.notice import ChannelCapabilities
from oc8.constants import ACME_TENANT_ID
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_context import append_resolved_approval, append_tool_call
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FnRuntime:
    """Wraps a bare async(db, **kw) -> RunResult callable as a RuntimeAdapter,
    for tests that inject a scripted result directly (bypassing resolve_runtime)."""

    def __init__(self, fn: Callable[..., Awaitable[RunResult]]) -> None:
        self._fn = fn

    async def execute(self, db: Any, **kw: Any) -> RunResult:
        return await self._fn(db, **kw)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def _make_queued_run(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as s:
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Dev",
        )
        s.add(agent)
        await s.flush()
        repo = RunRepository(s)
        run = await repo.create(tenant_id=tenant, agent_id=agent.id, context={"task": "do it"})
        return run.id


async def test_execute_run_marks_done(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="finished",
            tool_calls=[],
            steps=2,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.DONE.value
        assert run.context["output"] == "finished"
        assert run.context["steps"] == 2
        # A finished run frees the agent back to idle for the office view.
        agent = await s.get(m.Agent, run.agent_id)
        assert agent is not None
        assert agent.status == "idle"


async def test_execute_run_marks_failed_on_error(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)

    async def boom(db: Any, **kw: Any) -> RunResult:
        raise RuntimeError("kaboom")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(boom),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.FAILED.value
        assert "kaboom" in run.context["error"]
        # A failed run must leave a trace an operator can actually see --
        # previously this was silent everywhere (Activity, agent Overview).
        events = (
            (
                await s.execute(
                    select(m.ActivityEvent).where(
                        m.ActivityEvent.agent_id == run.agent_id,
                        m.ActivityEvent.status == "error",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert "kaboom" in (events[0].detail or "")


async def test_execute_run_marks_failed_when_agent_missing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        repo = RunRepository(s)
        created = await repo.create(
            tenant_id=tenant, agent_id=uuid.uuid4(), context={"task": "do it"}
        )
        run_id = created.id

    async def unused_runner(db: Any, **kw: Any) -> RunResult:
        raise AssertionError("runner should not be called when agent is missing")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(unused_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.FAILED.value
        assert "agent not found" in run.context["error"]
        events = (
            (
                await s.execute(
                    select(m.ActivityEvent).where(
                        m.ActivityEvent.agent_id == run.agent_id,
                        m.ActivityEvent.status == "error",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert "agent not found" in events[0].message


async def test_execute_run_marks_failed_on_unknown_mcp_connection(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    unknown_mcp_id = uuid.uuid4()
    async with app_session(tenant) as s:
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Dev",
        )
        s.add(agent)
        await s.flush()
        repo = RunRepository(s)
        created = await repo.create(
            tenant_id=tenant,
            agent_id=agent.id,
            context={"task": "do it", "mcp_connection_id": str(unknown_mcp_id)},
        )
        run_id = created.id

    async def unused_runner(db: Any, **kw: Any) -> RunResult:
        raise AssertionError("runner should not be called for an unknown MCP connection")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(unused_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.FAILED.value
        assert "unknown MCP connection" in run.context["error"]
        events = (
            (
                await s.execute(
                    select(m.ActivityEvent).where(
                        m.ActivityEvent.agent_id == run.agent_id,
                        m.ActivityEvent.status == "error",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert "unknown MCP connection" in events[0].message


async def test_execute_run_marks_failed_on_unknown_status(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)

    async def weird_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="weird",
            output="huh",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(weird_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.FAILED.value
        assert "unknown run status" in run.context["error"]
        events = (
            (
                await s.execute(
                    select(m.ActivityEvent).where(
                        m.ActivityEvent.agent_id == run.agent_id,
                        m.ActivityEvent.status == "error",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert "unknown run status" in events[0].message


async def test_execute_run_marks_waiting_for_approval_on_budget_exceeded(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)

    async def budget_runner(db: Any, **kw: Any) -> RunResult:
        # Mirrors what run_agent does on a hard budget-exceeded stop: it
        # pauses the agent itself before returning the RunResult.
        kw["agent"].status = "paused"
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="budget_exceeded",
            output="budget exceeded",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(budget_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        # budget_exceeded is resumable, not a terminal failure.
        assert run.state == RunState.WAITING_FOR_APPROVAL.value
        assert "error" not in run.context
        # The agent's "paused" status (set by run_agent) must survive, not
        # get clobbered back to "idle" by the executor.
        agent = await s.get(m.Agent, run.agent_id)
        assert agent is not None
        assert agent.status == "paused"


async def test_execute_run_dispatches_to_resolved_stub_runtime(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        # Unique semver suffix: ACME_TENANT_ID is shared across the whole
        # suite, and install_plugin raises DuplicateVersionError on a
        # repeated (plugin_id, semver) for the same plugin name — see the
        # identical note in tests/runtime/test_registry.py's
        # _install_stub_runtime helper.
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data={
                "name": "oc8.echo-runtime-stub",
                "version": f"1.0.0+{uuid.uuid4().hex[:8]}",
                "type": "runtime_adapter",
                "trust": "community",
                "capabilities": ["streaming"],
            },
        )
        await enable_plugin(s, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Dev",
            runtime_ref=str(version.capa_id),
        )
        s.add(agent)
        await s.flush()
        repo = RunRepository(s)
        run = await repo.create(tenant_id=tenant, agent_id=agent.id, context={"task": "hi"})
        run_id = run.id

    # No `runtime=` override: exercises the real resolve_runtime path.
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False)
    )

    async with app_session(tenant) as s:
        refreshed = await s.get(m.AgentRun, run_id)
        assert refreshed is not None
        assert refreshed.state == RunState.DONE.value
        assert "echo-runtime-stub" in refreshed.context["output"]


async def test_execute_run_marks_failed_on_stale_runtime_ref(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Dev",
            runtime_ref=str(uuid.uuid4()),
        )
        s.add(agent)
        await s.flush()
        repo = RunRepository(s)
        run = await repo.create(tenant_id=tenant, agent_id=agent.id, context={"task": "hi"})
        run_id = run.id

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False)
    )

    async with app_session(tenant) as s:
        refreshed = await s.get(m.AgentRun, run_id)
        assert refreshed is not None
        assert refreshed.state == RunState.FAILED.value
        assert "RuntimeResolutionError" in refreshed.context["error"]


async def test_execute_run_threads_parent_task_id_and_depth_from_context(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    parent_id = uuid.uuid4()
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        repo = RunRepository(s)
        created = await repo.create(
            tenant_id=tenant,
            agent_id=agent.id,
            context={
                "task": "sub work",
                "parent_task_id": str(parent_id),
                "delegation_depth": 2,
            },
        )
        run_id = created.id

    seen: dict[str, Any] = {}

    async def capturing_runner(db: Any, **kw: Any) -> RunResult:
        seen.update(kw)
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="ok",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(capturing_runner),
    )

    assert seen["parent_task_id"] == parent_id
    assert seen["delegation_depth"] == 2


class _VisibilityCheckingQueue:
    """Records enqueues, asserting at enqueue time that the run row is already
    durably visible from a brand-new session — i.e. the commit happened BEFORE
    the publish. The worker's XREADGROUP can return the instant a stream entry
    exists, so a row not yet committed would be silently dropped. Mirrors the
    guard in tests/api/test_run_endpoint.py."""

    def __init__(self, app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
        self._app_session = app_session
        self._tenant = tenant
        self.enqueued: list[uuid.UUID] = []

    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        async with self._app_session(self._tenant) as s:
            row = await s.get(m.AgentRun, run_id)
            assert row is not None, "run must be committed before it is published"
        self.enqueued.append(run_id)


async def test_execute_run_publishes_pending_runs_after_commit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)
    queue = _VisibilityCheckingQueue(app_session, tenant)
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)

    async def delegating_runner(db: Any, **kw: Any) -> RunResult:
        sub = await RunRepository(db).create(
            tenant_id=kw["tenant_id"],
            agent_id=kw["agent"].id,
            context={"task": "sub"},
            source="delegation",
        )
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="delegated",
            tool_calls=[],
            steps=1,
            pending_runs=[sub.id],
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(delegating_runner),
    )

    assert len(queue.enqueued) == 1
    async with app_session(tenant) as s:
        sub = await s.get(m.AgentRun, queue.enqueued[0])
        assert sub is not None
        assert sub.state == RunState.QUEUED.value


async def test_execute_run_publishes_nothing_when_no_pending_runs(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)
    queue = _VisibilityCheckingQueue(app_session, tenant)
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)

    async def plain_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="ok",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(plain_runner),
    )

    assert queue.enqueued == []


async def test_execute_run_interrupts_when_cancel_requested_mid_flight(
    app_session: AppSessionFactory,
) -> None:
    """A run_cancellation row inserted by a SEPARATE committed session (as the
    cancel endpoint does) is seen by the executor's own long transaction under
    READ COMMITTED, and the run ends INTERRUPTED with the agent idle. Because the
    signal lives off the locked agent_run tuple, this does NOT deadlock (the old
    cancel_requested_at column, on the agent_run row the executor holds locked,
    did)."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)

    async def cancelling_runner(db: Any, **kw: Any) -> RunResult:
        check = kw["cancel_check"]
        assert await check() is False
        # A different committed session records the cancel mid-run.
        async with app_session(tenant) as other:
            other.add(
                m.RunCancellation(
                    tenant_id=tenant,
                    run_id=run_id,
                    requested_at=_now(),
                    cancellation_kind="operator_interrupted",
                )
            )
        assert await check() is True
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="interrupted",
            output="cancelled",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(cancelling_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.INTERRUPTED.value
        assert run.context["cancellation_kind"] == "operator_interrupted"
        agent = await s.get(m.Agent, run.agent_id)
        assert agent is not None and agent.status == "idle"


async def test_execute_run_skips_a_run_cancelled_while_queued(
    app_session: AppSessionFactory,
) -> None:
    """Cancelled while still queued: execute_run honors the run_cancellation row
    BEFORE invoking the runtime -> the run ends INTERRUPTED, the agent is idle,
    and the runtime is never called (the stream entry acks on the normal return
    -> drained)."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)
    async with app_session(tenant) as s:
        s.add(
            m.RunCancellation(
                tenant_id=tenant,
                run_id=run_id,
                requested_at=_now(),
                cancellation_kind="operator_interrupted",
            )
        )

    async def must_not_run(db: Any, **kw: Any) -> RunResult:
        raise AssertionError("a cancelled queued run must not invoke the runtime")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(must_not_run),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value
        assert run.context["cancellation_kind"] == "operator_interrupted"
        agent = await s.get(m.Agent, run.agent_id)
        assert agent is not None and agent.status == "idle"


async def test_a_decision_committed_while_the_run_finishes_survives(
    app_session: AppSessionFactory,
) -> None:
    """The operator approves while the run is still going, and the executor's own
    write must not erase that.

    Found by review during the runtime-hardening slice, never reproduced in the
    wild because it needs the decision to land inside a window of milliseconds:
    the executor refetches the run when the adapter returns, then flushes
    `{**run.context, output/steps}` built from THAT snapshot. Anything committed
    to the same JSONB column in between -- `resolved_tool_approvals` is the one
    that matters -- is silently written back out. The operator sees "approved",
    and nothing ever happens. `run_cancellation` and `run_message` are side
    tables precisely to dodge this; the approvals list never got that treatment.
    """
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)
    entry = {"sig": "create_record\n{}", "tool": "create_record", "decision": "approve"}

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        # Release the executor's row lock first -- both real container runtimes
        # commit here for exactly this reason, and without it the concurrent
        # write below simply blocks on us and the deadlock hides the bug. The
        # GUC is transaction-local, so re-pin it exactly as they do; RLS fails
        # closed otherwise and the failure looks nothing like this test's point.
        await db.commit()
        await db.execute(
            sql_text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant)}
        )
        # A SEPARATE session, committing mid-run: what the approvals endpoint
        # does while a long-lived runtime is still driving the container.
        async with app_session(tenant) as api_db:
            run = await api_db.get(m.AgentRun, run_id)
            assert run is not None
            await append_resolved_approval(api_db, run, entry)
            await api_db.commit()
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="finished",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["output"] == "finished", "the executor's own write must still land"
        assert run.context["resolved_tool_approvals"] == [entry], "the decision was overwritten"


async def test_append_tool_call_is_visible_before_the_run_finishes(
    app_session: AppSessionFactory,
) -> None:
    """The actual bug (live-observed 2026-08-25): a run's `toolCalls` never
    appeared in `agent_run.context` until the whole run ended -- a browser
    refresh mid-run showed an empty list even ten steps in, because the
    only write was executor.py's own merge_context() call after run_agent()
    already returned everything. append_tool_call lets the in-process
    engine persist each call as it happens instead."""
    tenant = uuid.uuid4()
    run_id = await _make_queued_run(app_session, tenant)
    first = {"tool": "search_record", "arguments": {"model": "res.partner"}, "result": "3 found"}
    second = {"tool": "get_record", "arguments": {"model": "res.partner", "id": 3}, "result": "ok"}

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        await append_tool_call(s, run, first)
        await s.commit()

    # A page load between steps -- a FRESH session, the same as a real
    # browser's GET /runs/{id} would use -- must already see the first call.
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context.get("toolCalls") == [first], (
            "a mid-run read must see the tool call already, not just the final one"
        )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        await append_tool_call(s, run, second)
        await s.commit()

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context.get("toolCalls") == [first, second], (
            "appends accumulate in order -- neither call overwrites the other"
        )


async def test_append_tool_call_survives_the_executors_final_write(
    app_session: AppSessionFactory,
) -> None:
    """The incremental appends during the run must not be erased by
    executor.py's own merge_context({"toolCalls": result.tool_calls, ...})
    once run_agent() returns -- that write is still the authoritative one
    for "output"/"steps", but it must not regress toolCalls to a stale
    snapshot the runtime captured before the last append landed."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _make_queued_run(app_session, tenant)
    live_call = {"tool": "search_record", "arguments": {}, "result": "ok"}

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        # Same session `execute_run` is already using -- exactly how the
        # real engine.py's _live_tool_call does it (append_tool_call(db, ...)
        # on the one session threaded through run_agent, never a second
        # session). A second `app_session(tenant)` opened HERE, nested
        # inside execute_run's own still-open transaction on this same
        # agent_run row, deadlocks against it instead of testing anything
        # real -- this is a test-construction bug, not a production one.
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        await append_tool_call(db, run, live_call)
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="finished",
            tool_calls=[live_call],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["toolCalls"] == [live_call]
        assert run.context["output"] == "finished"


# --- Task 8: routing a terminal chat run's reply to Telegram ---


class _RecordingChannels:
    """Fake `oc8.channels.registry.channels_for_tenant`: records, from a
    BRAND NEW session opened at the moment it is called, what state the run
    is already in -- the same technique `_VisibilityCheckingQueue` above
    uses for `pending_runs`. `_tell_telegram` is only ever called from the
    post-commit loop in `execute_run` (never from inside the still-open
    `tenant_session` block), so this must always observe a terminal state,
    never `RUNNING`/`QUEUED`. A regression that moved the Telegram send back
    inside the transaction would show up here as a state observed before
    the transition committed.

    Deliberately does not assert inside `_channels_for_tenant` itself:
    `_tell_telegram` wraps the `channels_for_tenant` call in a bare
    `except Exception`, so an `AssertionError` raised from in here would be
    silently swallowed and logged rather than failing the test. Observations
    are recorded instead and asserted on afterwards, outside executor.py's
    try/except entirely.
    """

    def __init__(
        self,
        app_session: AppSessionFactory,
        tenant: uuid.UUID,
        run_id: uuid.UUID,
        *,
        channel: str = "telegram",
        max_classification: str = "internal",
    ) -> None:
        self._app_session = app_session
        self._tenant = tenant
        self._run_id = run_id
        self._channel = channel
        self.observed_states: list[str | None] = []
        self.said: list[tuple[str, str]] = []
        self.raise_on_say = False
        # Defaults to "internal", not `ChannelCapabilities`'s real "public"
        # default: this fixture exists to test the reply-routing/timing
        # behaviour, not the classification gate (see
        # test_the_classification_gate_applies_to_telegram_replies_too below),
        # so it starts cleared for the `internal` replies these tests send.
        self._max_classification = max_classification

    async def _channels_for_tenant(self, db: Any, *, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with self._app_session(self._tenant) as check:
            run = await check.get(m.AgentRun, self._run_id)
            self.observed_states.append(run.state if run is not None else None)
        return {self._channel: self}

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(max_classification=self._max_classification)

    async def say(self, external_id: str, text: str) -> None:
        if self.raise_on_say:
            raise RuntimeError("telegram is down")
        self.said.append((external_id, text))


async def _make_chat_run(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    chat_channel: str | None,
    chat_channel_external_id: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A `source="chat"` run against a real `ChatSession`, the same shape
    `oc8.channels.dispatch.bind_from_free_text` and the ordinary web chat
    endpoint both produce. Returns (run_id, session_id)."""
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
        s.add(session)
        await s.flush()
        session_id = session.id
        context: dict[str, Any] = {"task": "hi", "chat_session_id": str(session_id)}
        if chat_channel is not None:
            context["chat_channel"] = chat_channel
        if chat_channel_external_id is not None:
            context["chat_channel_external_id"] = chat_channel_external_id
        repo = RunRepository(s)
        created = await repo.create(
            tenant_id=tenant, agent_id=agent.id, context=context, source="chat"
        )
        return created.id, session_id


async def test_a_terminal_chat_run_with_telegram_context_gets_the_reply_after_commit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _session_id = await _make_chat_run(
        app_session, tenant, chat_channel="telegram", chat_channel_external_id="555"
    )
    fake = _RecordingChannels(app_session, tenant, run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    assert fake.said == [("555", "Es sind 3 offen.")]
    assert fake.observed_states == [RunState.DONE.value], (
        "the reply must be sent only after the run's DONE transition is durably "
        "committed and visible from another session, never before"
    )


async def test_a_terminal_chat_run_on_a_non_telegram_channel_gets_the_reply_on_that_channel(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the generalization: the reply must go to whatever
    channel the run's own context names, not to a channel hardcoded in
    executor.py. Uses "teams" specifically so a regression that quietly
    re-hardcodes "telegram" fails loudly here."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _session_id = await _make_chat_run(
        app_session, tenant, chat_channel="teams", chat_channel_external_id="tg-777"
    )
    fake = _RecordingChannels(app_session, tenant, run_id, channel="teams")
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    assert fake.said == [("tg-777", "Es sind 3 offen.")]


async def test_a_terminal_chat_runs_reply_is_withheld_on_a_channel_left_at_the_default(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_RecordingChannels` above defaults to `max_classification="internal"`
    so the reply-routing tests aren't also classification tests. This one
    builds it at `ChannelCapabilities`'s real default -- `"public"`, what a
    channel actually starts at before an operator widens it -- and proves
    `_tell_telegram` applies the same gate `bind_from_free_text`'s ack does,
    not just a real answer's content but the whole reason this hook exists:
    a run's output is exactly the kind of material an approval's `detail`
    already gets held back for at this ceiling."""
    from oc8.channels.dispatch import _CONTENT_WITHHELD

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _session_id = await _make_chat_run(
        app_session, tenant, chat_channel="telegram", chat_channel_external_id="555"
    )
    fake = _RecordingChannels(app_session, tenant, run_id, max_classification="public")
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    assert fake.said == [("555", _CONTENT_WITHHELD)]


async def test_a_telegram_send_failure_does_not_fail_the_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _session_id = await _make_chat_run(
        app_session, tenant, chat_channel="telegram", chat_channel_external_id="555"
    )
    fake = _RecordingChannels(app_session, tenant, run_id)
    fake.raise_on_say = True
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(), agent_id=kw["agent"].id, status="done", output="ok",
            tool_calls=[], steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.DONE.value, "a Telegram send failure must not fail the run"


async def test_an_ordinary_chat_run_without_telegram_context_never_touches_telegram(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, session_id = await _make_chat_run(
        app_session, tenant, chat_channel=None, chat_channel_external_id=None
    )
    calls: list[uuid.UUID] = []

    async def must_not_be_called(db: Any, *, tenant_id: uuid.UUID) -> dict[str, Any]:
        calls.append(tenant_id)
        return {}

    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", must_not_be_called)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(), agent_id=kw["agent"].id, status="done", output="ok web reply",
            tool_calls=[], steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    assert calls == [], "no chat_channel in context -> channels_for_tenant must never run"
    # The ordinary (non-Telegram) reply path is unaffected either way.
    async with app_session(tenant) as s:
        messages = (
            (
                await s.execute(
                    select(m.ChatMessage).where(m.ChatMessage.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(messages) == 1
        assert messages[0].content == "ok web reply"


# --- a chat run that PARKS must still say something to its Telegram sender ---


async def test_a_chat_run_that_parks_for_clarification_tells_the_telegram_sender(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reply hook only fired on DONE/FAILED, so a run that stopped to ask
    a question went quiet after the "Bin dran" ack: the question was written to
    a Clarification the sender has no screen for, and nothing ever told them
    anything was expected of them."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _session_id = await _make_chat_run(
        app_session, tenant, chat_channel="telegram", chat_channel_external_id="556"
    )
    fake = _RecordingChannels(app_session, tenant, run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="waiting_for_input",
            output="Welche Rechnungsnummer?",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    assert len(fake.said) == 1
    external_id, text = fake.said[0]
    assert external_id == "556"
    assert "more information" in text
    assert fake.observed_states == [RunState.WAITING_FOR_INPUT.value], (
        "sent only after the park is durably committed, same rule as the terminal hook"
    )


async def test_a_chat_run_parked_on_an_approval_tells_the_telegram_sender(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other silent park: the run is sitting in somebody's approval queue,
    which the sender has no way to find out about from Telegram."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _session_id = await _make_chat_run(
        app_session, tenant, chat_channel="telegram", chat_channel_external_id="557"
    )
    fake = _RecordingChannels(app_session, tenant, run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="waiting_for_approval",
            output="",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    assert len(fake.said) == 1
    external_id, text = fake.said[0]
    assert external_id == "557"
    assert "approval" in text
    assert fake.observed_states == [RunState.WAITING_FOR_APPROVAL.value]


async def test_a_crashed_chat_run_still_records_a_reply_and_tells_telegram(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exception path transitions straight to FAILED and never called
    `record_assistant_reply`, so a crashed chat turn left the transcript with
    the user's message and nothing after it -- on the web too, not only on
    Telegram. The recorded reply is deliberately canned: `repr(exc)` is already
    on the run's `context["error"]` for an operator, and a chat transcript is
    the wrong place to show it."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, session_id = await _make_chat_run(
        app_session, tenant, chat_channel="telegram", chat_channel_external_id="558"
    )
    fake = _RecordingChannels(app_session, tenant, run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def fake_runner(db: Any, **kw: Any) -> RunResult:
        raise RuntimeError("the model provider hung up")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(fake_runner),
    )

    assert len(fake.said) == 1
    assert fake.said[0][0] == "558"
    assert fake.observed_states == [RunState.FAILED.value]

    async with app_session(tenant) as s:
        messages = (
            (
                await s.execute(
                    select(m.ChatMessage).where(m.ChatMessage.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(messages) == 1, "a crashed chat run left the transcript empty"
        assert "the model provider hung up" not in messages[0].content
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert "the model provider hung up" in run.context["error"]


def _make_inactive(tenant: uuid.UUID) -> None:
    from oc8.tenants.activity import set_tenant_activity_gate

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    set_tenant_activity_gate(gate)


async def _assert_interrupted_as_inactive(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID
) -> None:
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value
        assert run.context["cancellation_kind"] == "tenant_inactive"
        agent = await s.get(m.Agent, run.agent_id)
        assert agent is not None and agent.status == "idle"


async def _assert_the_stop_is_explained(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID
) -> None:
    """A run the gate stopped between steps says why, in its output and in the
    activity feed, and does not claim an operator cancelled it."""
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["output"] == "Cancelled: organisation is not active"
        events = await s.execute(
            select(m.ActivityEvent.message).where(m.ActivityEvent.tenant_id == tenant)
        )
        assert events.scalars().all() == ["Dev: Cancelled: organisation is not active"]


async def test_a_queued_run_of_an_inactive_organisation_is_interrupted_at_start(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    run_id = await _make_queued_run(app_session, tenant)
    _make_inactive(tenant)

    async def must_not_run(db: Any, **kw: Any) -> RunResult:
        raise AssertionError("a run of an inactive organisation must not invoke the runtime")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(must_not_run),
    )

    await _assert_interrupted_as_inactive(app_session, tenant, run_id)
    async with app_session(tenant) as s:
        events = await s.execute(
            select(m.ActivityEvent.message).where(m.ActivityEvent.tenant_id == tenant)
        )
        messages = events.scalars().all()
        assert "Dev: Cancelled: organisation is not active" in messages


async def test_a_republished_run_is_stopped_at_start_too(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.runtime.intake import publish_run
    from oc8.runtime.queue import RunQueue

    queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id = await _make_queued_run(app_session, tenant)
    _make_inactive(tenant)

    async def must_not_run(db: Any, **kw: Any) -> RunResult:
        raise AssertionError("a run of an inactive organisation must not invoke the runtime")

    try:
        await publish_run(run_id=run_id, tenant_id=tenant)
        message = await queue.dequeue(timeout=1.0)
        assert message is not None
        await execute_run(message, runtime=_FnRuntime(must_not_run))
    finally:
        await queue.close()

    await _assert_interrupted_as_inactive(app_session, tenant, run_id)


async def test_a_running_run_stops_at_its_next_step_when_the_organisation_stops(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result
    from oc8.runtime.adapter import Oc8AgentRuntime

    tenant = uuid.uuid4()

    class _CountingRouter:
        """Always asks for another tool call; the gate flips to "no" after the
        first model call, so a second call would mean the step check missed it."""

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, req: Any) -> CompletionResult:
            self.calls += 1
            _make_inactive(tenant)
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id=f"c{self.calls}", name="noop", arguments={})],
                usage=Usage(tokens_in=1, tokens_out=1),
                stop_reason="tool_use",
                provider="fake",
                model="fake",
            )

        async def stream(self, req: Any) -> Any:
            yield chunk_from_result(await self.complete(req))

    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)

    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        s.add(dept)
        await s.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Dev")
        s.add(agent)
        await s.flush()
        run = await RunRepository(s).create(
            tenant_id=tenant, agent_id=agent.id, context={"task": "loop"}
        )
        run_id = run.id

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=Oc8AgentRuntime(),
    )

    assert router.calls == 1
    await _assert_interrupted_as_inactive(app_session, tenant, run_id)
    await _assert_the_stop_is_explained(app_session, tenant, run_id)


async def test_a_raising_gate_lets_runs_continue(app_session: AppSessionFactory) -> None:
    from oc8.tenants.activity import set_tenant_activity_gate

    tenant = uuid.uuid4()
    run_id = await _make_queued_run(app_session, tenant)

    async def broken_gate(tenant_id: uuid.UUID) -> bool:
        raise RuntimeError("gate unavailable")

    set_tenant_activity_gate(broken_gate)

    async def stepping_runner(db: Any, **kw: Any) -> RunResult:
        assert await kw["cancel_check"]() is False
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="finished",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(stepping_runner),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.DONE.value
        assert "cancellation_kind" not in run.context


async def test_an_operator_cancel_wins_over_the_activity_gate(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    run_id = await _make_queued_run(app_session, tenant)

    async def cancelled_and_inactive(db: Any, **kw: Any) -> RunResult:
        async with app_session(tenant) as other:
            other.add(
                m.RunCancellation(
                    tenant_id=tenant,
                    run_id=run_id,
                    requested_at=_now(),
                    cancellation_kind="operator_interrupted",
                )
            )
        _make_inactive(tenant)
        assert await kw["cancel_check"]() is True
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="interrupted",
            output="Run cancelled by operator.",
            tool_calls=[],
            steps=1,
        )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(cancelled_and_inactive),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value
        assert run.context["cancellation_kind"] == "operator_interrupted"
        assert run.context["output"] == "Run cancelled by operator."


# ------------------------------------------------- container (isolated) runs


async def _make_queued_container_run(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        s.add(dept)
        await s.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Dev", narrowing={}, definition={}
        )
        s.add(agent)
        await s.flush()
        run = await RunRepository(s).create(
            tenant_id=tenant, agent_id=agent.id, context={"task": "loop"}
        )
        return agent.id, run.id


async def _run_in_a_scripted_container(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    tenant: uuid.UUID,
    *,
    before_the_step: Callable[[uuid.UUID], Awaitable[None]],
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Runs execute_run through the real DockerIsolatedRuntime. The container
    is replaced by what its shell does: ask /step once, then report the step's
    verdict to /finish -- the same calls isolated_shell makes."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.auth import get_identity_provider
    from oc8.config import get_settings
    from oc8.main import create_app
    from oc8.runtime.isolated import DockerIsolatedRuntime
    from tests.runtime.test_isolated_runtime import _FakeDriver

    monkeypatch.setenv("OC8_RUNTIME_SESSION_ROOT", str(tmp_path))
    get_settings.cache_clear()
    agent_id, run_id = await _make_queued_container_run(app_session, tenant)
    step: dict[str, Any] = {}

    async def shell() -> None:
        await before_the_step(run_id)
        token = get_identity_provider().mint(
            tenant_id=tenant,
            subject=f"agent:{agent_id}",
            role="agent_default",
            kind="agent",
            scopes=[f"run:{run_id}"],
        )
        headers = {"Authorization": f"Bearer {token}"}
        app = create_app()
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.post(f"/api/v1/internal/agent/{run_id}/step", headers=headers)
                assert r.status_code == 200, r.text
                step.update(r.json())
                assert step["tool_calls"] == []
                finished = await c.post(
                    f"/api/v1/internal/agent/{run_id}/finish",
                    json={"status": step["status_override"], "output": step["text"]},
                    headers=headers,
                )
                assert finished.status_code == 200, finished.text

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(shell))
    try:
        await execute_run(
            RunMessage(
                run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False
            ),
            runtime=DockerIsolatedRuntime(),
        )
    finally:
        get_settings.cache_clear()
    return run_id, step


def _cancel_by_operator(app_session: AppSessionFactory, tenant: uuid.UUID) -> Any:
    async def cancel(run_id: uuid.UUID) -> None:
        async with app_session(tenant) as other:
            other.add(
                m.RunCancellation(
                    tenant_id=tenant,
                    run_id=run_id,
                    requested_at=_now(),
                    cancellation_kind="operator_interrupted",
                )
            )

    return cancel


async def test_a_container_run_stops_at_its_next_step_when_the_organisation_stops(
    app_session: AppSessionFactory,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    tenant = uuid.uuid4()

    async def stop(run_id: uuid.UUID) -> None:
        _make_inactive(tenant)

    run_id, step = await _run_in_a_scripted_container(
        app_session, monkeypatch, tmp_path, tenant, before_the_step=stop
    )

    assert step["status_override"] == "interrupted"
    await _assert_interrupted_as_inactive(app_session, tenant, run_id)
    await _assert_the_stop_is_explained(app_session, tenant, run_id)
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        task = await s.get(m.Task, run.task_id)
        assert task is not None and task.state == "failed"


async def test_an_operator_cancel_stops_a_container_run_at_its_next_step(
    app_session: AppSessionFactory,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    tenant = uuid.uuid4()

    run_id, step = await _run_in_a_scripted_container(
        app_session,
        monkeypatch,
        tmp_path,
        tenant,
        before_the_step=_cancel_by_operator(app_session, tenant),
    )

    assert step["status_override"] == "interrupted"
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value
        assert run.context["cancellation_kind"] == "operator_interrupted"
        agent = await s.get(m.Agent, run.agent_id)
        assert agent is not None and agent.status == "idle"


async def test_an_operator_cancel_wins_over_the_activity_gate_in_a_container_run(
    app_session: AppSessionFactory,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    tenant = uuid.uuid4()
    cancel = _cancel_by_operator(app_session, tenant)

    async def both(run_id: uuid.UUID) -> None:
        await cancel(run_id)
        _make_inactive(tenant)

    run_id, _ = await _run_in_a_scripted_container(
        app_session, monkeypatch, tmp_path, tenant, before_the_step=both
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value
        assert run.context["cancellation_kind"] == "operator_interrupted"
