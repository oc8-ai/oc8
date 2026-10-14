"""Fix round 1 for A3/A4: `enqueue_run` is not the only door a run row comes
through. A delegated sub-run (`control_tools._delegate`) and a lead's wake-up
(`executor._maybe_wake_parent`) call `RunRepository.create` directly, and an
unpinned run resolves to the agent's CURRENT version on every read -- so a
publish landing mid-run reached it. `RunRepository.create` now pins, so every
creation path is covered by one funnel.

Also covered here: the two remaining live reads the review found for a run
already in flight -- memory READS (`retrieve_context`) and runtime selection
(`resolve_runtime`)."""

from __future__ import annotations

import uuid
from typing import Any

from oc8 import models as m
from oc8.agent.control_tools import _delegate, execute_control_tool
from oc8.agent.engine import RunResult
from oc8.agents.versioning import publish_version, resolve_version
from oc8.authz.pdp import Decision, Effect
from oc8.constants import ACME_TENANT_ID
from oc8.memory.router import retrieve_context
from oc8.modelrouter.types import ToolCall
from oc8.runtime.executor import _maybe_wake_parent, execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.registry import BUILTIN_IN_PROCESS_RUNTIME_REF, BUILTIN_ISOLATED_RUNTIME_REF
from oc8.runtime.repository import RunRepository
from tests.conftest import AppSessionFactory


async def _lead_and_sub(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Agent, m.Task]:
    dept = m.Department(tenant_id=tenant, name="Eng", frame={})
    db.add(dept)
    await db.flush()
    lead = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Lead", is_team_lead=True, mission="lead"
    )
    sub = m.Agent(tenant_id=tenant, department_id=dept.id, name="Ada", mission="original")
    db.add_all([lead, sub])
    await db.flush()
    await publish_version(db, lead)
    await publish_version(db, sub)
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=lead.id,
        title="big job",
        state="in_progress",
        delegation_depth=0,
    )
    db.add(task)
    await db.flush()
    return lead, sub, task


async def test_repository_create_pins_the_current_version(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _lead, sub, _task = await _lead_and_sub(db, tenant)
        run = await RunRepository(db).create(tenant_id=tenant, agent_id=sub.id, context={})
        assert run.agent_version_id is not None
        assert run.agent_version_id == sub.current_version_id


async def test_a_delegated_sub_run_is_pinned_and_a_later_publish_does_not_reach_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, sub, task = await _lead_and_sub(db, tenant)
        _out, sub_run_id = await _delegate(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="1",
                name="delegate_task",
                arguments={"agent_id": str(sub.id), "task_text": "do the thing"},
            ),
            mcp_conn=None,
            run_id=None,
        )
        assert sub_run_id is not None
        sub_run = await db.get(m.AgentRun, sub_run_id)
        assert sub_run is not None
        assert sub_run.agent_version_id is not None
        assert sub_run.agent_version_id == sub.current_version_id

        # Published while the delegated run is queued/in flight.
        sub.mission = "changed mid-run"
        await publish_version(db, sub)
        pinned = await resolve_version(db, sub_run, sub)
    assert pinned["mission"] == "original"


async def test_a_lead_wake_run_is_pinned_and_a_later_publish_does_not_reach_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, sub, task = await _lead_and_sub(db, tenant)
        wake_id = await _maybe_wake_parent(
            db,
            repo=RunRepository(db),
            tenant_id=tenant,
            parent_task_id=task.id,
            delegation_depth=1,
            finished_agent_id=sub.id,
            sub_task_label="the sub work",
            output="done",
            succeeded=True,
            mcp_conn=None,
            run_context=None,
        )
        assert wake_id is not None
        wake = await db.get(m.AgentRun, wake_id)
        assert wake is not None
        assert wake.agent_version_id is not None
        assert wake.agent_version_id == lead.current_version_id

        lead.mission = "changed mid-run"
        await publish_version(db, lead)
        pinned = await resolve_version(db, wake, lead)
    assert pinned["mission"] == "lead"


# ------------------------------------------------------------- memory reads

MEMORY_FRAME: dict[str, Any] = {"memory": {"department": ["read", "write"]}}
#: A draft on the live row: department memory narrowed to write-only.
NO_DEPT_READ: dict[str, Any] = {"memory": {"department": ["write"]}}


async def _memory_agent(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task]:
    dept = m.Department(tenant_id=tenant, name="Eng", frame=MEMORY_FRAME)
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A", narrowing={})
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="t",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    return agent, task


def _spy_reads(monkeypatch: Any) -> list[tuple[str, dict[str, Any]]]:
    import oc8.memory.router as router_mod
    from oc8.memory.policy import authorize_memory_read as real

    seen: list[tuple[str, dict[str, Any]]] = []

    def _spy(frame: dict[str, Any], narrowing: dict[str, Any], tier: str) -> bool:
        seen.append((tier, narrowing))
        return real(frame, narrowing, tier)

    monkeypatch.setattr(router_mod, "authorize_memory_read", _spy)
    return seen


async def test_retrieve_context_gates_tiers_on_the_narrowing_it_is_given(
    app_session: AppSessionFactory, monkeypatch: Any
) -> None:
    seen = _spy_reads(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task = await _memory_agent(db, tenant)
        agent.narrowing = NO_DEPT_READ  # unpublished draft on the live row
        await retrieve_context(
            db, agent=agent, tenant_id=tenant, frame=MEMORY_FRAME, query_text="x", narrowing={}
        )
    assert seen and all(n == {} for _t, n in seen), seen


async def test_search_memory_reads_with_the_pinned_narrowing(
    app_session: AppSessionFactory, monkeypatch: Any
) -> None:
    """`search_memory` mid-run: the pinned version allows department reads,
    the live draft does not -- the pinned version must win, as it already does
    for memory_write."""
    seen = _spy_reads(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _memory_agent(db, tenant)
        pinned: dict[str, Any] = {"narrowing": {}, "model_config_id": None}
        agent.narrowing = NO_DEPT_READ
        await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="search_memory", arguments={"query": "anything"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            pinned=pinned,
        )
    dept_reads = [n for t, n in seen if t == "department"]
    assert dept_reads == [{}], seen


# ---------------------------------------------------------- runtime selection


async def test_the_executor_picks_the_runtime_of_the_pinned_version(
    app_session: AppSessionFactory, monkeypatch: Any
) -> None:
    """An unpublished runtime assignment must not move a run already pinned
    (e.g. one re-executed on resume) onto a different runtime."""
    import oc8.runtime.executor as executor_mod

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name=f"Eng {uuid.uuid4().hex[:6]}", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name=f"Runner {uuid.uuid4().hex[:6]}",
            runtime_ref=BUILTIN_IN_PROCESS_RUNTIME_REF,
        )
        db.add(agent)
        await db.flush()
        await publish_version(db, agent)
        run = await RunRepository(db).create(
            tenant_id=tenant, agent_id=agent.id, context={"task": "do it"}
        )
        # Draft only -- never published.
        agent.runtime_ref = BUILTIN_ISOLATED_RUNTIME_REF
        run_id = run.id

    asked: list[Any] = []

    class _Done:
        async def execute(self, db: Any, **kw: Any) -> RunResult:
            return RunResult(uuid.uuid4(), kw["agent"].id, "done", "ok", [], 1)

    async def _fake_resolve(db: Any, **kw: Any) -> Any:
        asked.append(kw.get("runtime_ref"))
        return _Done()

    monkeypatch.setattr(executor_mod, "resolve_runtime", _fake_resolve)
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False)
    )
    assert asked == [BUILTIN_IN_PROCESS_RUNTIME_REF]


async def test_resolve_runtime_honours_an_explicit_runtime_ref(
    app_session: AppSessionFactory,
) -> None:
    from oc8.runtime.adapter import Oc8AgentRuntime
    from oc8.runtime.isolated import DockerIsolatedRuntime
    from oc8.runtime.registry import resolve_runtime

    tenant = uuid.uuid4()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=uuid.uuid4(),
        name="A",
        runtime_ref=BUILTIN_ISOLATED_RUNTIME_REF,
    )
    async with app_session(tenant) as db:
        live = await resolve_runtime(db, tenant_id=tenant, agent=agent)
        pinned = await resolve_runtime(
            db, tenant_id=tenant, agent=agent, runtime_ref=BUILTIN_IN_PROCESS_RUNTIME_REF
        )
    assert isinstance(live, DockerIsolatedRuntime)
    assert isinstance(pinned, Oc8AgentRuntime)
