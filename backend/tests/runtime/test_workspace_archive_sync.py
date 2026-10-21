"""Output sync for sandboxes without host mounts.

While a leg runs, `run_output_sandbox` keeps a per-run task that stores
`/workspace/output` every `run_output_sync_interval_s` in its own tenant
session; on exit it stops that task and takes one last pass before the
runtime tears the sandbox down. Each pass lists the files with `find`, skips
those already stored with the same name and size, and fetches the rest one by
one with `get_archive`, capped at the attachment limit. Archives are
untrusted: they are unpacked only through `safe_extract_to_memory`, and
nothing from them is written to the host."""

from __future__ import annotations

import asyncio
import io
import os
import posixpath
import tarfile
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
    run_output_sandbox,
    sanitize_output_name,
    sync_sandbox_output,
)
from oc8.sandbox.types import (
    ArchiveTooLarge,
    BindMount,
    ExecResult,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
    ScratchDir,
)
from oc8.storage.attachments import MAX_ATTACHMENT_BYTES
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

MOUNTLESS = SandboxSpec(image="img", scratch=(ScratchDir("/workspace", 64),))
WITH_MOUNTS = SandboxSpec(
    image="img",
    mounts=[BindMount(host_path="/tmp/x", container_path="/workspace", readonly=False)],
)
HANDLE = SandboxHandle(container_id="c0ffee", image="img")


@pytest.fixture(autouse=True)
def _use_tmp_session_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("OC8_RUNTIME_SESSION_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeDriver:
    """A sandbox whose /workspace/output holds ``files``. ``exec`` answers the
    listing command as GNU find would; ``get_archive`` returns a one-file tar,
    or a symlink entry for names in ``symlinks``. A file is "virtually" large
    via ``sizes`` without holding its bytes."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.sizes: dict[str, int] = {}
        self.symlinks: set[str] = set()
        self.listing: str | None = None
        self.exec_error: Exception | None = None
        self.fetches: list[tuple[str, int | None]] = []
        self.execs: list[list[str]] = []
        self.torn_down = False

    def _size(self, name: str) -> int:
        return self.sizes.get(name, len(self.files.get(name, b"")))

    async def exec(
        self,
        handle: SandboxHandle,
        command: list[str],
        *,
        workdir: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> ExecResult:
        self.execs.append(command)
        if self.torn_down:
            raise SandboxError("no such container")
        if self.exec_error is not None:
            raise self.exec_error
        if self.listing is not None:
            return ExecResult(exit_code=0, output=self.listing)
        names = sorted(set(self.files) | set(self.sizes))
        return ExecResult(exit_code=0, output="".join(f"{self._size(n)}\t{n}\0" for n in names))

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        rel = posixpath.relpath(path, OUTPUT_CONTAINER_PATH)
        self.fetches.append((rel, max_bytes))
        if self.torn_down:
            raise SandboxError("no such container")
        base = posixpath.basename(path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            if rel in self.symlinks:
                info = tarfile.TarInfo(base)
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/shadow"
                tar.addfile(info)
            else:
                if max_bytes is not None and self._size(rel) > max_bytes:
                    raise ArchiveTooLarge("too big")
                if rel not in self.files:
                    raise SandboxError(f"no such path: {path}")
                data = self.files[rel]
                info = tarfile.TarInfo(base)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    async def teardown(self, handle: SandboxHandle) -> None:
        self.torn_down = True


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


async def _attachments(db: Any, run_id: uuid.UUID) -> dict[str, int]:
    rows = (
        (await db.execute(select(m.FileAttachment).where(m.FileAttachment.owner_id == run_id)))
        .scalars()
        .all()
    )
    return {r.filename: r.size_bytes for r in rows}


def _host_output_exists(run_id: uuid.UUID) -> bool:
    return os.path.exists(output_dir(run_id))


# --- one pass ---------------------------------------------------------------


async def test_output_from_the_sandbox_becomes_attachments(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"report.txt": b"Q3 numbers look good.", "charts/q3.csv": b"a,b\n1,2\n"})
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        assert sorted(f.filename for f in synced) == ["charts/q3.csv", "report.txt"]
        rows = (
            (await db.execute(select(m.FileAttachment).where(m.FileAttachment.owner_id == run.id)))
            .scalars()
            .all()
        )
        by_name = {r.filename: r for r in rows}
        assert by_name["report.txt"].extracted_text == "Q3 numbers look good."
        assert by_name["report.txt"].owner_type == "agent_run"

    # One file per fetch, each capped at the attachment limit plus tar overhead.
    assert sorted(name for name, _ in driver.fetches) == ["charts/q3.csv", "report.txt"]
    for _name, cap in driver.fetches:
        assert cap is not None and MAX_ATTACHMENT_BYTES < cap <= MAX_ATTACHMENT_BYTES + 65536
    assert not _host_output_exists(run.id)


async def test_the_listing_is_a_bounded_find_of_the_output_dir(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"a.txt": b"a"})
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        await sync_sandbox_output(db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE)
    script = " ".join(driver.execs[0])
    assert "find /workspace/output" in script
    assert "-type f" in script
    assert "%s\\t%P\\0" in script
    assert f"head -z -n {get_settings().run_output_max_files + 1}" in script


async def test_files_already_stored_with_the_same_size_are_not_fetched_again(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"report.txt": b"same"})
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        first = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        second = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        driver.files["report.txt"] = b"longer now"
        third = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
    assert [f.filename for f in first] == ["report.txt"]
    assert second == []
    assert [f.size_bytes for f in third] == [len(b"longer now")]
    assert [name for name, _ in driver.fetches] == ["report.txt", "report.txt"]


async def test_a_large_file_is_skipped_and_the_small_one_still_stored(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"small.txt": b"fits"})
    driver.sizes["huge.txt"] = MAX_ATTACHMENT_BYTES + 1  # listed, never held
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        assert [f.filename for f in synced] == ["small.txt"]
    # The large file is skipped from its listed size and never fetched.
    assert [name for name, _ in driver.fetches] == ["small.txt"]


async def test_a_file_that_grew_past_the_cap_after_listing_is_skipped(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"grows.txt": b"x", "ok.txt": b"ok"})
    driver.listing = "1\tgrows.txt\0" + "2\tok.txt\0"
    driver.sizes["grows.txt"] = MAX_ATTACHMENT_BYTES + (1 << 17)
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        assert [f.filename for f in synced] == ["ok.txt"]


@pytest.mark.parametrize(
    "listing",
    ["1\t../../etc/cron.d/x\0", "1\t/etc/passwd\0", "1\ta/../../x\0", "garbage\0", "x\ty\0"],
    ids=["dotdot", "absolute", "climb", "no-tab", "bad-size"],
)
async def test_an_unsafe_or_malformed_listing_entry_is_skipped(
    app_session: AppSessionFactory, minio_url: str, listing: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"ok.txt": b"ok"})
    driver.listing = listing + "2\tok.txt\0"
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        assert [f.filename for f in synced] == ["ok.txt"]
    assert [name for name, _ in driver.fetches] == ["ok.txt"]
    assert not _host_output_exists(run.id)


async def test_a_file_swapped_for_a_symlink_is_skipped(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"ok.txt": b"ok", "link.txt": b"x"})
    driver.symlinks.add("link.txt")
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        assert [f.filename for f in synced] == ["ok.txt"]


async def test_only_the_first_max_files_entries_are_considered(
    app_session: AppSessionFactory, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OC8_RUN_OUTPUT_MAX_FILES", "2")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    driver = _FakeDriver({f"f{i}.txt": b"x" for i in range(4)})
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        assert len(synced) == 2


@pytest.mark.parametrize(
    "error", [SandboxError("no such container"), RuntimeError("boom")], ids=["sandbox", "other"]
)
async def test_a_failed_listing_syncs_nothing_and_does_not_raise(
    app_session: AppSessionFactory, minio_url: str, error: Exception
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"a.txt": b"a"})
    driver.exec_error = error
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        assert (
            await sync_sandbox_output(
                db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
            )
            == []
        )


async def test_one_failing_file_does_not_stop_the_others_or_break_the_session(
    app_session: AppSessionFactory, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.storage import attachments

    real = attachments.store_attachment_bytes

    async def flaky(db: Any, **kw: Any) -> Any:
        if kw["filename"] == "bad.txt":
            raise RuntimeError("storage hiccup")
        return await real(db, **kw)

    monkeypatch.setattr(workspace, "store_attachment_bytes", flaky)
    tenant = uuid.uuid4()
    driver = _FakeDriver({"bad.txt": b"b", "good.txt": b"g"})
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        assert [f.filename for f in synced] == ["good.txt"]
        # The session is still usable afterwards (the executor transitions on it).
        assert await _attachments(db, run.id) == {"good.txt": 1}


# --- names --------------------------------------------------------------------


async def test_names_lose_control_characters_and_are_capped() -> None:
    assert sanitize_output_name("re\x1bport\n.txt") == "report.txt"
    assert sanitize_output_name("a/b‮/c.txt") == "a/b/c.txt"
    long = "x" * 400 + ".txt"
    capped = sanitize_output_name(long)
    assert capped is not None
    assert len(capped.encode()) <= 255
    assert capped.endswith(".txt")
    multibyte = "ü" * 300 + ".md"
    capped_mb = sanitize_output_name(multibyte)
    assert capped_mb is not None
    assert len(capped_mb.encode()) <= 255
    assert capped_mb.endswith(".md")
    capped_mb.encode().decode()  # still valid UTF-8
    # Each segment fits an object-store key segment after the uuid prefix.
    deep = "/".join(["d" * 150] * 3) + "/" + "f" * 250 + ".txt"
    capped_deep = sanitize_output_name(deep)
    assert capped_deep is not None
    assert len(capped_deep.encode()) <= 255
    assert all(len(seg.encode()) <= 200 for seg in capped_deep.split("/"))
    assert capped_deep.endswith(".txt")
    assert sanitize_output_name("\x01\x02") is None


async def test_long_and_odd_names_are_stored_sanitised(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    long = "n" * 300 + ".txt"
    driver = _FakeDriver({long: b"long", "tab\there.txt": b"tab", "new\nline.txt": b"nl"})
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_sandbox_output(
            db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
        )
        names = sorted(f.filename for f in synced)
        assert "tabhere.txt" in names
        assert "newline.txt" in names
        assert any(n.endswith(".txt") and len(n.encode()) <= 255 for n in names if n[0] == "n")
        assert len(names) == 3
        # A second pass recognises the sanitised names and stores nothing new.
        assert (
            await sync_sandbox_output(
                db, tenant_id=tenant, run_id=run.id, driver=driver, handle=HANDLE
            )
            == []
        )


# --- the per-run task -----------------------------------------------------------


async def test_output_is_stored_while_the_block_is_still_running(
    app_session: AppSessionFactory, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's housekeeping cannot run while a run executes in it, so the
    sync has to be the run's own task."""
    ticks: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        ticks.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(workspace, "_sleep", fake_sleep)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
    driver = _FakeDriver({"progress.md": b"half way"})

    async with run_output_sandbox(run.id, tenant, MOUNTLESS, driver, HANDLE):
        for _ in range(200):
            async with app_session(tenant) as db:
                if await _attachments(db, run.id):
                    break
            await asyncio.sleep(0.01)
        async with app_session(tenant) as db:
            assert await _attachments(db, run.id) == {"progress.md": len(b"half way")}
    assert ticks and ticks[0] == get_settings().run_output_sync_interval_s


async def test_the_last_pass_happens_on_exit_before_teardown(
    app_session: AppSessionFactory, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(seconds: float) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(workspace, "_sleep", never)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
    driver = _FakeDriver({"final.txt": b"last words"})
    async with run_output_sandbox(run.id, tenant, MOUNTLESS, driver, HANDLE):
        pass
    await driver.teardown(HANDLE)
    async with app_session(tenant) as db:
        assert await _attachments(db, run.id) == {"final.txt": len(b"last words")}


async def test_the_last_pass_runs_when_the_block_raises(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
    driver = _FakeDriver({"partial.txt": b"so far"})
    with pytest.raises(RuntimeError):
        async with run_output_sandbox(run.id, tenant, MOUNTLESS, driver, HANDLE):
            raise RuntimeError("leg crashed")
    async with app_session(tenant) as db:
        assert await _attachments(db, run.id) == {"partial.txt": len(b"so far")}


async def test_the_last_pass_is_bounded_by_a_timeout(
    app_session: AppSessionFactory, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OC8_RUN_OUTPUT_FINAL_READ_TIMEOUT_S", "0.05")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)

    class _Hanging(_FakeDriver):
        async def exec(self, *a: Any, **kw: Any) -> ExecResult:
            await asyncio.Event().wait()
            raise AssertionError

    async with asyncio.timeout(5):
        async with run_output_sandbox(run.id, tenant, MOUNTLESS, _Hanging(), HANDLE):
            pass


async def test_a_sandbox_with_mounts_starts_no_task_and_reads_nothing(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    driver = _FakeDriver({"x.txt": b"x"})
    async with run_output_sandbox(uuid.uuid4(), tenant, WITH_MOUNTS, driver, HANDLE):
        await asyncio.sleep(0)
    assert driver.execs == [] and driver.fetches == []


async def test_the_task_is_gone_after_the_block(
    app_session: AppSessionFactory, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(workspace, "_sleep", fake_sleep)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
    driver = _FakeDriver({"a.txt": b"a"})
    before = {t for t in asyncio.all_tasks() if not t.done()}
    async with run_output_sandbox(run.id, tenant, MOUNTLESS, driver, HANDLE):
        await asyncio.sleep(0.05)
    after = {t for t in asyncio.all_tasks() if not t.done()}
    assert after <= before | {asyncio.current_task()}
    calls = len(driver.execs)
    await asyncio.sleep(0.05)
    assert len(driver.execs) == calls


# --- the executor -------------------------------------------------------------------


class _FnRuntime:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def execute(self, db: Any, **kw: Any) -> RunResult:
        return await self._fn(db, **kw)


async def _queued_run(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        return (
            await RunRepository(s).create(
                tenant_id=tenant, agent_id=agent.id, context={"task": "do it"}
            )
        ).id


async def test_output_is_synced_when_the_run_fails(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    """Review focus: output arrives even when the runtime raises."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _queued_run(app_session, tenant)
    driver = _FakeDriver({"partial.txt": b"got this far"})

    async def crashing_runtime(db: Any, **kw: Any) -> RunResult:
        try:
            async with run_output_sandbox(kw["run_id"], kw["tenant_id"], MOUNTLESS, driver, HANDLE):
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


def _write_host_output(run_id: uuid.UUID, name: str, content: bytes) -> None:
    root = output_dir(run_id)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, name), "wb") as fh:
        fh.write(content)


def _asking(run_id_box: list[uuid.UUID], extra: Any = None) -> Any:
    async def runtime(db: Any, **kw: Any) -> RunResult:
        if extra is not None:
            await extra(kw)
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="waiting_for_input",
            output="Which quarter?",
            tool_calls=[],
            steps=1,
        )

    return runtime


async def test_a_leg_that_waits_for_input_keeps_its_sandbox_output(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _queued_run(app_session, tenant)
    driver = _FakeDriver({"draft.md": b"first half"})

    async def leg(kw: dict[str, Any]) -> None:
        try:
            async with run_output_sandbox(kw["run_id"], kw["tenant_id"], MOUNTLESS, driver, HANDLE):
                pass
        finally:
            await driver.teardown(HANDLE)

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(_asking([], leg)),
    )
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.WAITING_FOR_INPUT.value
        assert await _attachments(s, run_id) == {"draft.md": len(b"first half")}


async def test_a_leg_that_waits_for_input_syncs_its_host_output(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id = await _queued_run(app_session, tenant)

    async def leg(kw: dict[str, Any]) -> None:
        _write_host_output(kw["run_id"], "notes.txt", b"from the mount")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(_asking([], leg)),
    )
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.WAITING_FOR_INPUT.value
        assert await _attachments(s, run_id) == {"notes.txt": len(b"from the mount")}
