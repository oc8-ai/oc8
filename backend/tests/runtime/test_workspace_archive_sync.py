"""Output sync for sandboxes without host mounts: `/workspace/output` is read
with `get_archive`, unpacked in memory through `safe_extract_to_memory`, and
stored as attachments exactly like the host-walk path does. A sandbox with
mounts (Nanoclaw) keeps the host walk. The last read happens when the runtime
lets go of its sandbox, before teardown, so the executor's terminal sync (on
success and on failure) still sees the files."""

from __future__ import annotations

import io
import os
import tarfile
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.config import get_settings
from oc8.constants import ACME_TENANT_ID
from oc8.runtime import workspace
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from oc8.runtime.workspace import (
    OUTPUT_CONTAINER_PATH,
    output_dir,
    release_run_sandbox,
    run_output_sandbox,
    sync_active_run_outputs,
    sync_run_output,
    track_run_sandbox,
)
from oc8.sandbox.types import (
    ArchiveTooLarge,
    BindMount,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
    ScratchDir,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

MOUNTLESS = SandboxSpec(image="img", scratch=(ScratchDir("/workspace", 64),))


@pytest.fixture(autouse=True)
def _use_tmp_session_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("OC8_RUNTIME_SESSION_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _tar(entries: list[tuple[str, bytes | None, str]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = (content or b"").decode()
                tar.addfile(info)
            else:
                data = content or b""
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _host_output_exists(run_id: uuid.UUID) -> bool:
    return os.path.exists(output_dir(run_id))


def _write_host_output(run_id: uuid.UUID, name: str, content: bytes) -> None:
    root = output_dir(run_id)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, name), "wb") as fh:
        fh.write(content)


class _FakeDriver:
    """Only what the output sync touches: get_archive, plus a teardown flag
    after which the sandbox is gone."""

    def __init__(self, archive: bytes | Exception) -> None:
        self.archive = archive
        self.calls: list[tuple[str, str, int | None]] = []
        self.torn_down = False

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        self.calls.append((handle.container_id, path, max_bytes))
        if self.torn_down:
            raise SandboxError("no such container")
        if isinstance(self.archive, Exception):
            raise self.archive
        return self.archive

    async def teardown(self, handle: SandboxHandle) -> None:
        self.torn_down = True


HANDLE = SandboxHandle(container_id="c0ffee", image="img")


async def _dept_agent_run(db: Any, tenant: uuid.UUID, *, state: str = "running") -> m.AgentRun:
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Ada",
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state=state)
    db.add(run)
    await db.flush()
    return run


async def _attachments(db: Any, run_id: uuid.UUID) -> dict[str, bytes | int]:
    rows = (
        (await db.execute(select(m.FileAttachment).where(m.FileAttachment.owner_id == run_id)))
        .scalars()
        .all()
    )
    return {r.filename: r.size_bytes for r in rows}


async def test_output_from_the_archive_becomes_attachments(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver(
        _tar(
            [
                ("report.txt", b"Q3 numbers look good.", "file"),
                ("charts", None, "dir"),
                ("charts/q3.csv", b"a,b\n1,2\n", "file"),
            ]
        )
    )
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        assert track_run_sandbox(run.id, MOUNTLESS, driver, HANDLE) is True
        try:
            synced = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        finally:
            workspace._live_sandboxes.pop(run.id, None)

        assert sorted(f.filename for f in synced) == ["charts/q3.csv", "report.txt"]
        rows = (
            (await db.execute(select(m.FileAttachment).where(m.FileAttachment.owner_id == run.id)))
            .scalars()
            .all()
        )
        by_name = {r.filename: r for r in rows}
        assert by_name["report.txt"].extracted_text == "Q3 numbers look good."
        assert by_name["report.txt"].owner_type == "agent_run"

    assert driver.calls == [("c0ffee", OUTPUT_CONTAINER_PATH, get_settings().run_output_max_bytes)]
    # Nothing from the sandbox ever touched the host.
    assert not _host_output_exists(run.id)


async def test_a_repeated_pass_skips_files_already_synced(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver(_tar([("report.txt", b"same", "file")]))
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        track_run_sandbox(run.id, MOUNTLESS, driver, HANDLE)
        try:
            first = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
            second = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        finally:
            workspace._live_sandboxes.pop(run.id, None)
    assert [f.filename for f in first] == ["report.txt"]
    assert second == []


async def test_a_sandbox_with_mounts_keeps_the_host_walk(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver(_tar([("from-archive.txt", b"no", "file")]))
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        spec = SandboxSpec(
            image="img",
            mounts=[BindMount(host_path="/tmp/x", container_path="/workspace", readonly=False)],
        )
        assert track_run_sandbox(run.id, spec, driver, HANDLE) is False
        _write_host_output(run.id, "from-host.txt", b"yes")

        synced = await sync_run_output(db, tenant_id=tenant, run_id=run.id)

    assert [f.filename for f in synced] == ["from-host.txt"]
    assert driver.calls == []


@pytest.mark.parametrize(
    "entries",
    [
        [("ok.txt", b"fine", "file"), ("../../etc/cron.d/x", b"evil", "file")],
        [("ok.txt", b"fine", "file"), ("/etc/passwd", b"evil", "file")],
        [("ok.txt", b"fine", "file"), ("link.txt", b"/etc/shadow", "symlink")],
    ],
    ids=["dotdot", "absolute", "symlink"],
)
async def test_an_unsafe_archive_syncs_nothing_and_does_not_raise(
    app_session: AppSessionFactory,
    minio_url: str,
    entries: list[tuple[str, bytes | None, str]],
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver(_tar(entries))
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        track_run_sandbox(run.id, MOUNTLESS, driver, HANDLE)
        try:
            synced = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        finally:
            workspace._live_sandboxes.pop(run.id, None)
        assert synced == []
        assert await _attachments(db, run.id) == {}
    assert not _host_output_exists(run.id)


async def test_too_many_output_files_syncs_nothing(
    app_session: AppSessionFactory, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OC8_RUN_OUTPUT_MAX_FILES", "2")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    driver = _FakeDriver(_tar([(f"f{i}.txt", b"x", "file") for i in range(3)]))
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        track_run_sandbox(run.id, MOUNTLESS, driver, HANDLE)
        try:
            assert await sync_run_output(db, tenant_id=tenant, run_id=run.id) == []
        finally:
            workspace._live_sandboxes.pop(run.id, None)


@pytest.mark.parametrize(
    "error",
    [ArchiveTooLarge("too big"), SandboxError("no such path: /workspace/output")],
    ids=["too-large", "missing-dir"],
)
async def test_a_failed_archive_read_syncs_nothing_and_does_not_raise(
    app_session: AppSessionFactory, minio_url: str, error: Exception
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver(error)
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        track_run_sandbox(run.id, MOUNTLESS, driver, HANDLE)
        try:
            assert await sync_run_output(db, tenant_id=tenant, run_id=run.id) == []
        finally:
            workspace._live_sandboxes.pop(run.id, None)


async def test_the_last_read_happens_before_teardown_and_is_synced_after_it(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    """The runtime tears its sandbox down before it returns; the executor syncs
    after that. The read taken when the runtime lets go is what the executor's
    sync stores, once."""
    tenant = uuid.uuid4()
    driver = _FakeDriver(_tar([("final.txt", b"last words", "file")]))
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        async with run_output_sandbox(run.id, MOUNTLESS, driver, HANDLE):
            pass
        await driver.teardown(HANDLE)

        synced = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        assert [f.filename for f in synced] == ["final.txt"]
        # Consumed: a later pass neither re-reads the gone sandbox nor
        # re-stores the snapshot.
        calls = len(driver.calls)
        assert await sync_run_output(db, tenant_id=tenant, run_id=run.id) == []
        assert len(driver.calls) == calls


async def test_releasing_a_sandbox_with_mounts_reads_nothing() -> None:
    driver = _FakeDriver(_tar([("x.txt", b"x", "file")]))
    run_id = uuid.uuid4()
    spec = SandboxSpec(
        image="img",
        mounts=[BindMount(host_path="/tmp/x", container_path="/workspace", readonly=False)],
    )
    async with run_output_sandbox(run_id, spec, driver, HANDLE):
        pass
    await release_run_sandbox(run_id)  # a second release is harmless
    assert driver.calls == []
    assert run_id not in workspace._final_outputs


async def _new_tenant_with_org(app_session: AppSessionFactory) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"test-{tenant.hex[:12]}",
                name="Test Org",
                tier="standard",
                region="eu",
            )
        )
        await db.flush()
    return tenant


async def test_the_periodic_sync_reads_a_live_sandbox(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    driver = _FakeDriver(_tar([("progress.md", b"half way", "file")]))
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
    track_run_sandbox(run.id, MOUNTLESS, driver, HANDLE)
    try:
        assert await sync_active_run_outputs() >= 1
    finally:
        workspace._live_sandboxes.pop(run.id, None)
    async with app_session(tenant) as db:
        assert await _attachments(db, run.id) == {"progress.md": len(b"half way")}


async def test_the_periodic_sync_leaves_the_last_read_to_the_executor(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    driver = _FakeDriver(_tar([("final.txt", b"done", "file")]))
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
    async with run_output_sandbox(run.id, MOUNTLESS, driver, HANDLE):
        pass
    try:
        await sync_active_run_outputs()
        assert run.id in workspace._final_outputs
        async with app_session(tenant) as db:
            assert await _attachments(db, run.id) == {}
    finally:
        workspace._final_outputs.pop(run.id, None)


async def test_a_last_read_nobody_collected_is_dropped_after_a_while(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    run_id = uuid.uuid4()
    stale = time.monotonic() - workspace.FINAL_OUTPUT_TTL_S - 1
    workspace._final_outputs[run_id] = (stale, {"x.txt": b"x"})
    await sync_active_run_outputs()
    assert run_id not in workspace._final_outputs


# --- the executor's failure path -------------------------------------------


class _FnRuntime:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def execute(self, db: Any, **kw: Any) -> RunResult:
        return await self._fn(db, **kw)


async def test_output_is_synced_when_the_run_fails(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    """Review focus: output arrives even when the runtime raises. The runtime
    lets go of its sandbox (taking the last read) and tears it down on the way
    out; the executor's failure branch stores what was read."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        run_id = (
            await RunRepository(s).create(
                tenant_id=tenant, agent_id=agent.id, context={"task": "do it"}
            )
        ).id

    driver = _FakeDriver(_tar([("partial.txt", b"got this far", "file")]))

    async def crashing_runtime(db: Any, **kw: Any) -> RunResult:
        try:
            async with run_output_sandbox(kw["run_id"], MOUNTLESS, driver, HANDLE):
                raise RuntimeError("kaboom")
        finally:
            await driver.teardown(HANDLE)

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(crashing_runtime),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.FAILED.value
        assert await _attachments(s, run_id) == {"partial.txt": len(b"got this far")}
    assert run_id not in workspace._final_outputs


async def test_the_executor_releases_a_sandbox_the_runtime_did_not(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    """A runtime that tracked its sandbox but never released it: the
    executor's terminal sync takes the read itself and stops tracking, so
    nothing is left behind in this process."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        run_id = (
            await RunRepository(s).create(
                tenant_id=tenant, agent_id=agent.id, context={"task": "do it"}
            )
        ).id

    driver = _FakeDriver(_tar([("left.txt", b"still here", "file")]))

    async def forgetful_runtime(db: Any, **kw: Any) -> RunResult:
        track_run_sandbox(kw["run_id"], MOUNTLESS, driver, HANDLE)
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
        runtime=_FnRuntime(forgetful_runtime),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.DONE.value
        assert await _attachments(s, run_id) == {"left.txt": len(b"still here")}
    assert run_id not in workspace._live_sandboxes
    assert run_id not in workspace._final_outputs
