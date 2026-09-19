"""The isolated-shell runtime's first-ever filesystem mount: a per-run host
directory bound at `/workspace`, synced to `FileAttachment` by
`oc8.runtime.workspace.sync_run_output` once the run ends (see
isolated.py's `execute`). Verifies the `SandboxSpec` handed to `provision()`
carries it -- and that `validate_mounts`'s traversal guard actually runs, by
using the fixture other isolated-runtime tests already establish
(test_isolated_runtime.py's `_FakeDriver`/`_agent_and_run`)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from oc8 import models as m
from oc8.config import get_settings
from oc8.runtime.isolated import DockerIsolatedRuntime
from oc8.runtime.states import RunState
from oc8.runtime.workspace import workspace_root
from oc8.sandbox.types import SandboxHandle
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _use_tmp_session_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("OC8_RUNTIME_SESSION_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _SpyDriver:
    def __init__(self) -> None:
        self.spec: Any = None

    async def provision(self, spec: Any) -> SandboxHandle:
        self.spec = spec
        return SandboxHandle(container_id="c" * 12, image=spec.image)

    async def wait(self, handle: SandboxHandle, timeout_s: float) -> int:
        return 0

    async def logs(self, handle: SandboxHandle) -> str:
        return ""

    async def teardown(self, handle: SandboxHandle) -> None:
        return None


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


async def test_provisions_a_workspace_bind_mount(
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

    assert driver.spec is not None
    mounts = driver.spec.mounts
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount.container_path == "/workspace"
    assert mount.readonly is False
    assert Path(mount.host_path) == Path(workspace_root(run_id))
    # Docker would otherwise auto-create the bind source as root:root, which
    # the container's unprivileged uid can't write to -- see isolated.py.
    assert Path(mount.host_path).is_dir()  # noqa: ASYNC240 -- one-off existence check


async def test_workspace_host_path_stays_within_the_session_root(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """validate_mounts's traversal guard runs on this mount too -- not just
    documented as intent. A run_id can't smuggle a `..` (UUIDs can't), so
    this pins the *shape* of the call (host_path inside runtime_session_root)
    rather than trying to force a traversal through a UUID primary key."""
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

    session_root = Path(get_settings().runtime_session_root)
    host_path = Path(driver.spec.mounts[0].host_path)
    assert host_path.is_relative_to(session_root)
