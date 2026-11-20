"""A run's /workspace kept between legs (without output/, which is stored as
attachments), and every state object of a run deleted once the run ends."""

from __future__ import annotations

import io
import logging
import tarfile
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.config import get_settings
from oc8.runtime.run_state import RunStateStore, pack_files
from oc8.runtime.workspace_state import (
    WORKSPACE_STATE,
    load_workspace,
    save_workspace,
)
from oc8.sandbox.types import ArchiveTooLarge, SandboxHandle
from tests.conftest import AppSessionFactory

_HANDLE = SandboxHandle(container_id="c1", image="i1")


class _Driver:
    def __init__(self, files: dict[str, bytes], *, too_large: bool = False) -> None:
        self.files = files
        self.too_large = too_large
        self.calls: list[tuple[str, int | None]] = []

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        self.calls.append((path, max_bytes))
        if self.too_large:
            raise ArchiveTooLarge("too big")
        return pack_files(self.files)


async def test_the_workspace_is_saved_without_output_and_loads_back(minio_url: str) -> None:
    tenant, run = uuid.uuid4(), uuid.uuid4()
    driver = _Driver({"notes.txt": b"n", "src/a.py": b"a", "output/r.pdf": b"r"})

    assert await save_workspace(driver, _HANDLE, tenant_id=tenant, run_id=run)  # type: ignore[arg-type]

    assert driver.calls == [("/workspace", get_settings().run_workspace_state_max_bytes)]
    assert await load_workspace(tenant_id=tenant, run_id=run) == {
        "notes.txt": b"n",
        "src/a.py": b"a",
    }


async def test_a_workspace_over_its_cap_is_dropped_with_a_warning(
    minio_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Never fails the leg. An earlier leg's workspace is deleted too, so the
    next leg starts empty rather than from stale files."""
    tenant, run = uuid.uuid4(), uuid.uuid4()
    await RunStateStore(tenant).save(run, WORKSPACE_STATE, pack_files({"old.txt": b"o"}))

    with caplog.at_level(logging.WARNING):
        saved = await save_workspace(
            _Driver({}, too_large=True),  # type: ignore[arg-type]
            _HANDLE,
            tenant_id=tenant,
            run_id=run,
        )

    assert saved is False
    assert "next leg starts with an empty workspace" in caplog.text
    assert await load_workspace(tenant_id=tenant, run_id=run) is None


async def test_the_workspace_cap_is_its_own_setting(
    minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace may be larger than a CLI home; the store accepts it up
    to `run_workspace_state_max_bytes`, not `run_state_max_bytes`."""
    monkeypatch.setattr(get_settings(), "run_state_max_bytes", 64 * 1024)
    tenant, run = uuid.uuid4(), uuid.uuid4()
    big = b"x" * (256 * 1024)
    assert await save_workspace(
        _Driver({"big.bin": big}),  # type: ignore[arg-type]
        _HANDLE,
        tenant_id=tenant,
        run_id=run,
    )
    assert await load_workspace(tenant_id=tenant, run_id=run) == {"big.bin": big}


async def _run_row(db: Any, tenant: uuid.UUID) -> m.AgentRun:
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
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add(run)
    await db.flush()
    return run


@pytest.mark.parametrize("final", ["done", "failed", "interrupted"])
async def test_a_runs_state_is_deleted_when_it_ends(
    app_session: AppSessionFactory, minio_url: str, final: str
) -> None:
    from oc8.runtime import run_state
    from oc8.runtime.repository import RunRepository
    from oc8.runtime.states import RunState

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _run_row(db, tenant)
        run_id = run.id
    store = RunStateStore(tenant)
    await store.save(run_id, "claude-home", pack_files({"t.jsonl": b"{}"}))
    await store.save(run_id, WORKSPACE_STATE, pack_files({"notes.txt": b"n"}))

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        await RunRepository(db).transition(run, RunState(final))
    await run_state.drain_deletions()

    assert await store.load_files(run_id, "claude-home") is None
    assert await store.load_files(run_id, WORKSPACE_STATE) is None


async def test_a_waiting_run_keeps_its_state(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    from oc8.runtime import run_state
    from oc8.runtime.repository import RunRepository
    from oc8.runtime.states import RunState

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _run_row(db, tenant)
        run_id = run.id
    store = RunStateStore(tenant)
    await store.save(run_id, "claude-home", pack_files({"t.jsonl": b"{}"}))

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        await RunRepository(db).transition(run, RunState.WAITING_FOR_INPUT)
    await run_state.drain_deletions()

    assert await store.load_files(run_id, "claude-home") == {"t.jsonl": b"{}"}


async def test_a_failed_deletion_is_logged_not_raised(
    minio_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from oc8.runtime import run_state
    from oc8.storage import s3

    async def boom(prefix: str) -> int:
        raise RuntimeError("storage down")

    monkeypatch.setattr(s3, "delete_prefix", boom)
    with caplog.at_level(logging.WARNING):
        await run_state.delete_run_state(uuid.uuid4(), uuid.uuid4())
    assert "could not delete" in caplog.text


def _names(tar: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        return t.getnames()
