"""The isolated runtime's control-plane side: it opens the run's Task before the
container starts, so an approval raised mid-run resolves back to the run, and it
starts a resume leg on a clean transcript. The container itself is faked here --
the live round-trip needs Docker and is exercised end-to-end, not in this suite.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.config import get_settings
from oc8.runtime.approval_resume import resolve_tool_approval
from oc8.runtime.isolated import DockerIsolatedRuntime
from oc8.runtime.states import RunState
from oc8.sandbox.types import SandboxHandle
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _use_tmp_session_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """DockerIsolatedRuntime.execute now creates a per-run `/workspace` host
    directory under runtime_session_root (see isolated.py); the real default
    is a deployed container's `/var/lib/oc8`, not writable here."""
    monkeypatch.setenv("OC8_RUNTIME_SESSION_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeDriver:
    """Stands in for the Docker driver: `wait` runs `on_wait` in place of the
    container, so a test can script exactly what the shell would have written
    through the internal API (in its own session, as the container does)."""

    def __init__(self, on_wait: Any = None) -> None:
        self._on_wait = on_wait
        self.spec: Any = None

    async def provision(self, spec: Any) -> SandboxHandle:
        self.spec = spec
        return SandboxHandle(container_id="c" * 12, image=spec.image)

    async def wait(self, handle: SandboxHandle, timeout_s: float) -> int:
        if self._on_wait is not None:
            await self._on_wait()
        return 0

    async def logs(self, handle: SandboxHandle) -> str:
        return ""

    async def teardown(self, handle: SandboxHandle) -> None:
        return None


async def _agent_and_run(
    session: AppSessionFactory, tenant: uuid.UUID, *, context: dict[str, Any] | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Nora", status="running",
            narrowing={}, definition={},
        )
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id,
            state=RunState.RUNNING.value, context=context or {"task": "verkauf etwas"},
        )
        db.add(run)
        await db.flush()
        return agent.id, run.id


async def test_run_gets_a_task_so_its_approval_can_resume_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: without a Task the internal API raises an approval with a
    NULL task_id, and the approved run can never be found again -- it sits in
    waiting_for_approval forever."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)

    async def shell() -> None:
        # What the internal API's tool endpoint does when a call needs approval:
        # raise the approval against the run's task, record the suspend verdict.
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            db.add(
                m.ApprovalRequest(
                    tenant_id=tenant, agent_id=agent_id, task_id=run.task_id,
                    action_type="tool_send", status="pending",
                    title="Nora wants to call create_record", detail="over threshold",
                    payload={"tool": "create_record", "arguments": {"amount": 4000}},
                )
            )
            run.context = {
                **run.context,
                "isolated_result": {"status": "waiting_for_approval", "output": "over threshold"},
            }

    driver = _FakeDriver(shell)
    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: driver)

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )
    assert result.status == "waiting_for_approval"

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        # A real task, not a fabricated id: the row exists and the result agrees.
        task = await db.get(m.Task, run.task_id)
        assert task is not None
        assert result.task_id == task.id
        assert task.state == "waiting_for_approval"

        ar = (
            await db.execute(
                m.ApprovalRequest.__table__.select().where(
                    m.ApprovalRequest.agent_id == agent_id
                )
            )
        ).first()
        assert ar is not None and ar.task_id == task.id

        # The whole point: an operator's approval now finds its run and re-queues it.
        run.state = RunState.WAITING_FOR_APPROVAL.value
        await db.flush()
        approval = await db.get(m.ApprovalRequest, ar.id)
        assert approval is not None
        resumed = await resolve_tool_approval(db, approval=approval, decision="approve")
        assert resumed is not None and resumed.id == run_id
        assert resumed.state == RunState.QUEUED.value


async def test_resume_leg_starts_clean_on_the_resume_instruction(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On resume the container must not replay the suspended leg's transcript --
    it ends in an assistant tool_call that never got a tool result, which
    providers reject -- and must be driven by the resume instruction."""
    tenant = uuid.uuid4()
    stale = {
        "task": "verkauf etwas",
        "steps": 3,
        "transcript": [
            {"role": "user", "content": "verkauf etwas"},
            {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "name": "create_record", "arguments": {}}],
            },
        ],
        "isolated_result": {"status": "waiting_for_approval", "output": "over threshold"},
    }
    agent_id, run_id = await _agent_and_run(app_session, tenant, context=stale)

    seen: dict[str, Any] = {}

    async def shell() -> None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            # What the container would read when it takes its first step.
            seen["task"] = run.context.get("task")
            seen["transcript"] = run.context.get("transcript")
            seen["steps"] = run.context.get("steps")
            run.context = {
                **run.context,
                "isolated_result": {"status": "done", "output": "angebot erstellt"},
            }

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(shell))

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="FREIGEGEBEN — create_record", tenant_id=tenant,
            run_id=run_id, pre_decided={"create_record\n{}": "approve"},
        )

    assert seen["task"] == "FREIGEGEBEN — create_record"
    assert seen["transcript"] == []
    assert seen["steps"] == 0
    assert result.status == "done"

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        task = await db.get(m.Task, run.task_id)
        assert task is not None and task.state == "done"


async def test_first_leg_keeps_the_task_text_and_transcript(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without pre-decided approvals nothing is reset -- a plain run (or a
    re-delivered one mid-flight) keeps whatever the container has written."""
    tenant = uuid.uuid4()
    ctx = {"task": "verkauf etwas", "steps": 2, "transcript": [{"role": "user", "content": "x"}]}
    agent_id, run_id = await _agent_and_run(app_session, tenant, context=ctx)

    seen: dict[str, Any] = {}

    async def shell() -> None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            seen["transcript"] = run.context.get("transcript")
            seen["steps"] = run.context.get("steps")
            run.context = {**run.context, "isolated_result": {"status": "done", "output": "ok"}}

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(shell))

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )

    assert seen["transcript"] == [{"role": "user", "content": "x"}]
    assert seen["steps"] == 2


async def test_an_unknown_shell_verdict_fails_the_task(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No isolated_result at all (the container died before /finish) is a
    failure, and the task says so rather than staying in_progress."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)
    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver())

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )
    assert result.status == "failed"

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        task = await db.get(m.Task, run.task_id)
        assert task is not None and task.state == "failed"


async def test_a_hard_budget_breach_blocks_the_run_before_any_container(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§15.4 must bite under isolation too -- and before a container is
    provisioned, so a breaching tenant cannot spend on compute either."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        db.add(m.Budget(tenant_id=tenant, department_id=None, hard_limit_tokens=1000))
        db.add(
            m.TokenUsageRecord(
                tenant_id=tenant, request_id=uuid.uuid4(), model="opaas_ai:odoo-gpt",
                provider="openai_compatible", tokens_in=900, tokens_out=900,
                agent_id=agent_id, department_id=agent.department_id,
            )
        )

    provisioned: list[Any] = []

    class _RefusingDriver(_FakeDriver):
        async def provision(self, spec: Any) -> SandboxHandle:
            provisioned.append(spec)
            raise AssertionError("a container must not be provisioned over budget")

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _RefusingDriver())

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "budget_exceeded"
    assert provisioned == []

    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None and task.state == "budget_exceeded"
        # The scope is frozen and an operator has an incident to decide on.
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert agent.status == "paused" and agent.pause_reason == "budget"
        incident = (
            await db.execute(
                m.ApprovalRequest.__table__.select().where(
                    m.ApprovalRequest.tenant_id == tenant,
                    m.ApprovalRequest.action_type == "budget_incident",
                )
            )
        ).first()
        assert incident is not None


async def test_a_run_within_budget_still_starts(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must not block an ordinary run: a configured budget with room
    left leaves the container path untouched."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(m.Budget(tenant_id=tenant, department_id=None, hard_limit_tokens=1_000_000))

    async def shell() -> None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            run.context = {**run.context, "isolated_result": {"status": "done", "output": "ok"}}

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(shell))

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )
    assert result.status == "done"


async def test_resume_continues_the_suspended_task_instead_of_opening_a_second(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One suspend/resume cycle is ONE unit of work. Opening a second task would
    strand the suspended one in waiting_for_approval forever -- nothing ever
    revisits it -- so every approval would leave a permanent ghost on the board.
    """
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)

    async def suspend() -> None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            run.context = {
                **run.context,
                "isolated_result": {"status": "waiting_for_approval", "output": "over threshold"},
            }

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(suspend))
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        first = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )
    assert first.status == "waiting_for_approval"

    async def finish() -> None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            run.context = {**run.context, "isolated_result": {"status": "done", "output": "ok"}}

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(finish))
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        second = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="Fortsetzung nach Freigabe. FREIGEGEBEN — create_record",
            tenant_id=tenant, run_id=run_id, pre_decided={"create_record\n{}": "approve"},
        )

    assert second.task_id == first.task_id, "the resume leg must continue the same task"
    async with app_session(tenant) as db:
        tasks = (
            await db.execute(m.Task.__table__.select().where(m.Task.tenant_id == tenant))
        ).fetchall()
        assert len(tasks) == 1, "a second task would be an orphan on the board"
        # It closes out, and keeps what was ASKED as its title -- not the
        # internal resume instruction.
        assert tasks[0].state == "done"
        assert tasks[0].title == "Erstelle ein Angebot"


async def test_a_clarification_verdict_parks_the_run_and_task(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ask_user under isolation: the container records the verdict, the runtime
    surfaces it, and the executor's clarification branch takes it from there.
    Nothing produced this verdict before, so an isolated agent could never ask."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)

    async def asks() -> None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            run.context = {
                **run.context,
                "isolated_result": {
                    "status": "waiting_for_input",
                    "output": "Welches Konto soll ich nehmen?",
                },
            }

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(asks))
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="Erstelle ein Angebot", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "waiting_for_input"
    # The executor's clarification branch uses the output AS the question.
    assert result.output == "Welches Konto soll ich nehmen?"
    async with app_session(tenant) as db:
        task = await db.get(m.Task, result.task_id)
        assert task is not None and task.state == "waiting_for_input"


async def test_a_delegated_sub_run_reaches_the_executor_for_publishing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delegate_task under isolation creates the sub-run inside /tool, which can
    only record it on the run. If the runtime doesn't carry it into pending_runs
    the sub-run is created and never picked up -- a silently dropped delegation."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)
    sub_run_id = uuid.uuid4()

    async def delegates() -> None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            run.context = {
                **run.context,
                "pending_runs": [str(sub_run_id)],
                "isolated_result": {"status": "done", "output": "delegiert"},
            }

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(delegates))
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="Verteil die Arbeit", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "done"
    assert result.pending_runs == [sub_run_id]


async def test_a_rendered_component_reaches_the_run_result(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_component under isolation writes onto run.context inside /tool
    (internal_agent.py), the same way pending_runs does. Regression test for a
    bug where RunResult's rendered_components defaulted to [] here, so the
    executor's own merge_context() call silently overwrote the already-durable
    list with an empty one at the run's terminal commit -- a chat/cron agent
    that called render_component successfully still ended up with no durable
    record of it."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)
    rendered = {"component_key": "data_table", "props": {"title": "Tickets", "columns": []}}

    async def renders() -> None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            run.context = {
                **run.context,
                "rendered_components": [rendered],
                "isolated_result": {"status": "done", "output": "hier die tabelle"},
            }

    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: _FakeDriver(renders))
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="Zeig mir die Tickets", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "done"
    assert result.rendered_components == [rendered]


async def test_a_container_that_never_exits_is_torn_down_and_the_run_fails(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHAT CATCHES A WEDGE, now that a live worker holds its stream entry for as
    long as it works (worker._keep_claimed).

    Nothing else does, and nothing else should: while the container hangs the run
    IS being executed -- the worker is alive, its database heartbeat is honest,
    its claim renewal is honest. The one bound on it is this adapter's own
    `driver.wait(timeout_s=...)`, and the `finally` around it. That is a cap on
    the work rather than a guess about liveness, which is why it may say no.

    The reclaim never caught a wedge in the first place: on 2026-08-02 it failed
    run 019fc303 at five minutes and left oc8-nora-agent-019fc303 running. It
    closed the row and left the wedge; this closes the wedge.

    The run ROW is the other half. Measured live 2026-08-27 (the incident that
    prompted this test's rewrite): the executor's own `except Exception as exc:
    run_error = repr(exc)` catches whatever this raises and immediately writes
    `{"error": run_error}` onto THIS SAME session via `merge_context` -- and
    until now, `isolated.py` only re-bound the tenant GUC AFTER a *successful*
    wait, so that write ran unbound, RLS cast the empty setting to `""`, and
    `merge_context` itself crashed with `invalid input syntax for type uuid`.
    The real error was never recorded, `_process`'s catch-all logged only the
    secondary crash, and the run sat in "running" for the full ABANDONED_AFTER
    window before the reconciler (test_reconcile) or the reclaim
    (test_recovery) closed it -- indistinguishable, from the office view, from
    the agent having silently vanished. Fixed by moving the re-bind into the
    `finally` around provision/wait, so it runs on every exit path, not only
    the happy one; asserted below by calling `merge_context` on the very same
    session right after the raise, the same way the executor does.
    """
    from requests.exceptions import ReadTimeout

    from oc8.runtime.run_context import merge_context

    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)

    class _WedgedDriver(_FakeDriver):
        """A container that never exits: docker-py raises ReadTimeout when the
        wait request's own timeout expires (APIClient.wait documents exactly
        that), with the container still running."""

        def __init__(self) -> None:
            super().__init__(None)
            self.waited_with: float | None = None
            self.torn_down = False

        async def wait(self, handle: SandboxHandle, timeout_s: float) -> int:
            self.waited_with = timeout_s
            raise ReadTimeout("waited long enough")

        async def teardown(self, handle: SandboxHandle) -> None:
            self.torn_down = True

    driver = _WedgedDriver()
    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: driver)

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        with pytest.raises(ReadTimeout):
            await DockerIsolatedRuntime().execute(
                db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
            )

        # The tenant GUC survived the failure: the executor's own error-recording
        # write, on this same session, does not itself crash on an unbound RLS
        # GUC. Before the fix this raised `invalid input syntax for type uuid`.
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        patched = await merge_context(db, run, {"error": "provisioner request failed"})
        assert patched["error"] == "provisioner request failed"

    # The bound is real and finite -- an unbounded wait would make the wedge
    # permanent and this whole net imaginary.
    assert driver.waited_with is not None
    assert 0 < driver.waited_with < float("inf")
    # And the wedged container is gone, not merely disowned.
    assert driver.torn_down is True


async def test_container_wait_timeout_scales_with_the_agents_own_max_steps_override(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wall-clock cap on the container must track the SAME per-agent budget
    internal_agent.py's /step endpoint enforces (agent.engine._max_steps) --
    not the framework default alone -- or an operator raising one agent's step
    budget above the default would still get that agent's container killed on
    a timeout sized for the old, lower budget."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        agent.definition = {"max_steps": 500}

    class _RecordingDriver(_FakeDriver):
        def __init__(self) -> None:
            super().__init__(None)
            self.waited_with: float | None = None

        async def wait(self, handle: SandboxHandle, timeout_s: float) -> int:
            self.waited_with = timeout_s
            async with app_session(tenant) as db:
                run = await db.get(m.AgentRun, run_id)
                assert run is not None
                run.context = {**run.context, "isolated_result": {"status": "done", "output": "ok"}}
            return 0

    driver = _RecordingDriver()
    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: driver)

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        result = await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )

    assert result.status == "done"
    assert driver.waited_with == 500 * 60
