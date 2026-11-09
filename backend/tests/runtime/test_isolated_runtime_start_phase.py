"""The isolated runtime says what it is waiting for before the run's first
step: `agent_run.phase` is "provisioning" while the sandbox is being placed
(a new node, an image pull -- minutes on a cold cluster), "starting" once the
container runs and its shell has not asked for a step yet, and cleared at
the first step. Each change goes out as a `run.status` event so an open run
view shows it live (oc8.runtime.start_phase)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.runtime.isolated import DockerIsolatedRuntime
from oc8.runtime.start_phase import START_PHASES, clear_start_phase
from oc8.sandbox.types import ExecResult, SandboxHandle
from tests.conftest import AppSessionFactory
from tests.runtime.test_isolated_runtime_workspace import _agent_and_run

pytestmark = pytest.mark.asyncio


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(
        self, tenant_id: uuid.UUID, type_: str, data: dict[str, Any], *, source: str
    ) -> None:
        self.events.append((type_, data))


class _PhaseDriver:
    """Reads the run's phase at each point the runtime passes through."""

    def __init__(
        self, app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID
    ) -> None:
        self._session = app_session
        self._tenant = tenant
        self._run_id = run_id
        self.seen: dict[str, str | None] = {}
        #: Stands in for the shell asking for its first step before
        #: provision() has even returned.
        self.first_step_during_provision = False

    async def _phase(self) -> str | None:
        async with self._session(self._tenant) as db:
            run = await db.get(m.AgentRun, self._run_id)
            assert run is not None
            return run.phase

    async def get_archive(self, handle: SandboxHandle, path: str, **kw: Any) -> bytes:
        from oc8.runtime.run_state import pack_files

        return pack_files({})

    async def provision(self, spec: Any) -> SandboxHandle:
        self.seen["provision"] = await self._phase()
        if self.first_step_during_provision:
            await clear_start_phase(self._tenant, self._run_id)
        return SandboxHandle(container_id="c" * 12, image=spec.image)

    async def wait(self, handle: SandboxHandle, timeout_s: float) -> int:
        self.seen["wait"] = await self._phase()
        return 0

    async def logs(self, handle: SandboxHandle) -> str:
        return ""

    async def exec(self, handle: SandboxHandle, command: list[str], **kw: Any) -> ExecResult:
        return ExecResult(exit_code=0, output="")

    async def teardown(self, handle: SandboxHandle) -> None:
        return None


async def _execute(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, *, early_step: bool = False
) -> tuple[_PhaseDriver, _Bus, uuid.UUID, uuid.UUID]:
    tenant = uuid.uuid4()
    agent_id, run_id = await _agent_and_run(app_session, tenant)
    driver = _PhaseDriver(app_session, tenant, run_id)
    driver.first_step_during_provision = early_step
    bus = _Bus()
    monkeypatch.setattr("oc8.runtime.isolated.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("oc8.runtime.start_phase.get_event_bus", lambda: bus)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await DockerIsolatedRuntime().execute(
            db, agent=agent, task_text="verkauf etwas", tenant_id=tenant, run_id=run_id
        )
    return driver, bus, tenant, run_id


async def test_the_run_says_provisioning_then_starting_then_nothing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, bus, tenant, run_id = await _execute(app_session, monkeypatch)

    assert driver.seen == {"provision": "provisioning", "wait": "starting"}
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.phase is None
    phases = [data["phase"] for type_, data in bus.events if type_ == "run.status"]
    assert phases == ["provisioning", "starting", None]
    # The event carries the run's state as it is, so the live patcher keeps it.
    assert all(data["state"] == "running" for _t, data in bus.events)
    assert all(data["run_id"] == str(run_id) for _t, data in bus.events)


async def test_a_first_step_before_provision_returns_is_not_overwritten(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell can ask for its first step before provision() returns: the
    cleared phase must not come back as "starting" for the rest of the run."""
    driver, _bus, _tenant, _run_id = await _execute(app_session, monkeypatch, early_step=True)
    assert driver.seen["wait"] is None
    assert frozenset({"provisioning", "starting"}) == START_PHASES
