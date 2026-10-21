"""The isolated-shell runtime's `/workspace`: a scratch dir the driver
provides, with no host directory behind it. Output written under
`/workspace/output/` is read from the sandbox by `run_output_sandbox` while
the leg runs and once more before teardown (see isolated.py's `execute`)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.runtime.isolated import DockerIsolatedRuntime
from oc8.runtime.states import RunState
from oc8.sandbox.reaper import RUN_LABEL, TENANT_LABEL
from oc8.sandbox.types import ExecResult, SandboxHandle
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _SpyDriver:
    def __init__(self) -> None:
        self.spec: Any = None
        self.events: list[str] = []

    async def provision(self, spec: Any) -> SandboxHandle:
        self.spec = spec
        return SandboxHandle(container_id="c" * 12, image=spec.image)

    async def wait(self, handle: SandboxHandle, timeout_s: float) -> int:
        return 0

    async def logs(self, handle: SandboxHandle) -> str:
        return ""

    async def exec(self, handle: SandboxHandle, command: list[str], **kw: Any) -> ExecResult:
        # The output sync's listing: no files yet.
        self.events.append("list-output")
        return ExecResult(exit_code=0, output="uid=1000\0")

    async def teardown(self, handle: SandboxHandle) -> None:
        self.events.append("teardown")


async def _agent_and_run(
    session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            status="running",
            narrowing={},
            definition={},
        )
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state=RunState.RUNNING.value,
            context={"task": "verkauf etwas"},
        )
        db.add(run)
        await db.flush()
        return agent.id, run.id


async def test_provisions_a_scratch_workspace_and_no_mounts(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)

    driver = _SpyDriver()
    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: driver)

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )

    spec = driver.spec
    assert spec is not None
    assert spec.mounts == []
    assert [d.path for d in spec.scratch] == ["/workspace"]
    assert spec.scratch[0].size_mb > 0
    # Nothing to seed: the run token travels in the env, as before.
    assert spec.seed is None
    assert set(spec.env) == {"OC8_INTERNAL_URL", "OC8_AGENT_TOKEN", "OC8_RUN_ID"}
    assert spec.workdir == "/app"
    # The shell reaches the control plane's internal API; same as before.
    assert spec.egress == "open"
    assert spec.network_disabled is False


async def test_output_is_read_from_the_sandbox_before_teardown(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no host directory, the only place the output exists is the
    sandbox: the last output pass must run before teardown."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)

    driver = _SpyDriver()
    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: driver)

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )

    assert driver.events == ["list-output", "teardown"]


async def test_sandbox_carries_the_organisation_label(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)

    driver = _SpyDriver()
    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: driver)

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )

    assert driver.spec.labels[TENANT_LABEL] == str(tenant)
    assert driver.spec.labels[RUN_LABEL] == str(run_id)
