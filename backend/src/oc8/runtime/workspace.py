"""The `/workspace` host-path convention every containerized runtime shares,
and the sync step that turns a run's `/workspace/output/` into durable
`FileAttachment` rows.

Every containerized runtime plugin (`opencode_runtime`, `codex_runtime`,
`claude_code_runtime`, and -- via `oc8.runtime.isolated` -- isolated-shell)
binds a per-run host directory at exactly
`f"{settings.runtime_session_root}/{run_id}"` into its sandbox at
`/workspace`. That convention lives here, not inside any one plugin, so the
isolated runtime's own mount, this module's sync step, and
`api/v1/run.py`'s (legacy, being replaced) workspace-files endpoints all
agree on one definition instead of three copies that can drift.

`/workspace/output/` is the one reserved subdirectory an agent's produced
deliverables belong in; everything else under `/workspace` stays scratch
space, invisible to this sync and to the rest of oc8.

Two ways to read the output
---------------------------
* **Host walk.** A runtime whose sandbox bind-mounts the host directory
  (Nanoclaw, and any runtime not yet moved off mounts) is read by walking
  `output_dir(run_id)` on the host, as always.
* **Archive.** A sandbox without mounts has no host directory. The runtime
  hands its sandbox to this module with `run_output_sandbox` (or
  `track_run_sandbox` / `release_run_sandbox`); while it is tracked, a sync
  reads `/workspace/output` with `SandboxDriver.get_archive`, capped at
  `run_output_max_bytes`, and unpacks it in memory with
  `safe_extract_to_memory` (at most `run_output_max_files` members). The
  archive is untrusted, so nothing from it is ever written to the host; an
  unsafe or oversized archive makes that pass sync nothing.

  When the runtime lets go of the sandbox (before teardown, on success and on
  failure alike) one last read is taken and kept in memory until the
  executor's terminal `sync_run_output` stores it. The periodic sync leaves
  that last read alone, and drops one nobody collected after
  `FINAL_OUTPUT_TTL_S`.

  Which sandboxes are tracked is per process: the run worker executes runs
  and runs the periodic sync in the same process, so the sync reaches every
  sandbox that process's runs hold. Another worker sees no tracked sandbox for
  such a run and falls back to the host walk, which finds nothing.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import time
import uuid
from asyncio import to_thread
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.sandbox.archive import safe_extract_to_memory
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.types import SandboxError, SandboxHandle, SandboxSpec
from oc8.storage.attachments import AttachmentRejected, store_attachment_bytes

logger = logging.getLogger(__name__)

OUTPUT_SUBDIR = "output"
OUTPUT_CONTAINER_PATH = "/workspace/output"

#: How long a last read waits for the executor's terminal sync before the
#: periodic sync drops it (a run that never reached one, e.g. a cancelled task).
FINAL_OUTPUT_TTL_S = 15 * 60


@dataclass(frozen=True)
class _TrackedSandbox:
    driver: SandboxDriver
    handle: SandboxHandle


#: run id -> the live sandbox a sync reads with get_archive.
_live_sandboxes: dict[uuid.UUID, _TrackedSandbox] = {}
#: run id -> (monotonic time taken, files) of the read taken at release.
_final_outputs: dict[uuid.UUID, tuple[float, dict[str, bytes]]] = {}


def workspace_root(run_id: uuid.UUID) -> str:
    return os.path.join(get_settings().runtime_session_root, str(run_id))


def output_dir(run_id: uuid.UUID) -> str:
    return os.path.join(workspace_root(run_id), OUTPUT_SUBDIR)


def ensure_workspace_dir(path: str) -> str:
    """`mkdir -p` a host directory a sandbox will bind-mount, chowned to
    `settings.sandbox_user` when one is configured.

    Re-implements `cli_harness.dirs.ensure_dir`/`chown_to_sandbox_user`
    rather than importing them: `cli_harness` is a plugin-only helper never
    on core backend's own import path in production (see backend/pyproject
    .toml's `[tool.mypy]` note on that gap), and `oc8.runtime.isolated` is
    core backend, not a plugin.
    """
    os.makedirs(path, exist_ok=True)
    user = get_settings().sandbox_user
    if user:
        uid_s, _, gid_s = user.partition(":")
        uid = int(uid_s)
        gid = int(gid_s) if gid_s else -1
        try:
            os.chown(path, uid, gid)
        except OSError:
            logger.warning("could not chown workspace dir %s to %s", path, user)
    return path


def _scan_output_dir(root: str) -> list[tuple[str, bytes]]:
    """Blocking directory walk + read, run inside `asyncio.to_thread` by
    `sync_run_output` -- the same reasoning `api/v1/run.py`'s own
    `_list_workspace_files`/`_read_workspace_file` already documents for
    walking a sandbox's host directory off the event loop."""
    if not os.path.isdir(root):
        return []
    out: list[tuple[str, bytes]] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                with open(full, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append((rel, raw))
    return out


def track_run_sandbox(
    run_id: uuid.UUID, spec: SandboxSpec, driver: SandboxDriver, handle: SandboxHandle
) -> bool:
    """Read this run's output from its sandbox from now on.

    A spec with mounts is not tracked (returns False): its output is on the
    host and the host walk reads it. Call right after ``provision``; pair with
    ``release_run_sandbox`` before teardown, or use ``run_output_sandbox``.
    """
    if spec.mounts:
        return False
    _live_sandboxes[run_id] = _TrackedSandbox(driver, handle)
    _final_outputs.pop(run_id, None)
    return True


async def release_run_sandbox(run_id: uuid.UUID) -> None:
    """Take the last read of a tracked sandbox and stop tracking it.

    Call BEFORE teardown, also when the run failed: the read is kept for the
    executor's terminal ``sync_run_output``. Never raises; a no-op for a run
    that is not tracked.
    """
    tracked = _live_sandboxes.pop(run_id, None)
    if tracked is None:
        return
    files = await _read_output_archive(run_id, tracked)
    if files is not None:
        _final_outputs[run_id] = (time.monotonic(), files)


@asynccontextmanager
async def run_output_sandbox(
    run_id: uuid.UUID, spec: SandboxSpec, driver: SandboxDriver, handle: SandboxHandle
) -> AsyncIterator[None]:
    """``track_run_sandbox`` on entry, ``release_run_sandbox`` on exit, however
    the block ends. Tear the sandbox down only after leaving the block::

        handle = await driver.provision(spec)
        try:
            async with run_output_sandbox(run_id, spec, driver, handle):
                ...  # run the leg
        finally:
            await driver.teardown(handle)
    """
    track_run_sandbox(run_id, spec, driver, handle)
    try:
        yield
    finally:
        await release_run_sandbox(run_id)


async def _read_output_archive(
    run_id: uuid.UUID, tracked: _TrackedSandbox
) -> dict[str, bytes] | None:
    """The files under the sandbox's /workspace/output, or None when they
    cannot be read safely. Never raises: a missing directory, a gone sandbox,
    an oversized or unsafe archive are all logged and read as nothing."""
    settings = get_settings()
    try:
        data = await tracked.driver.get_archive(
            tracked.handle, OUTPUT_CONTAINER_PATH, max_bytes=settings.run_output_max_bytes
        )
        return safe_extract_to_memory(
            data,
            max_members=settings.run_output_max_files,
            max_total_bytes=settings.run_output_max_bytes,
        )
    except SandboxError as exc:
        logger.warning("run %s: could not read output from the sandbox: %s", run_id, exc)
    except Exception:
        logger.exception("run %s: reading output from the sandbox failed", run_id)
    return None


async def _collect_output(run_id: uuid.UUID, *, take_final: bool) -> list[tuple[str, bytes]] | None:
    """The run's output files, from wherever they are. None means "leave this
    run to the executor": the last read is waiting and ``take_final`` is off."""
    final = _final_outputs.get(run_id)
    if final is not None:
        if not take_final:
            return None
        del _final_outputs[run_id]
        return sorted(final[1].items())
    tracked = _live_sandboxes.get(run_id)
    if tracked is not None:
        files = await _read_output_archive(run_id, tracked)
        return sorted(files.items()) if files else []
    return await to_thread(_scan_output_dir, output_dir(run_id))


async def sync_run_output(
    db: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> list[m.FileAttachment]:
    """Upload every new/changed file under this run's `/workspace/output/`
    into `FileAttachment` (`owner_type="agent_run"`, `owner_id=run_id`).

    Safe to call repeatedly against the same run -- both from the terminal
    transition (once, when the run ends) and from the periodic active-run
    pass (`sync_active_run_outputs`, while it is still running): a file
    already attached under the same filename AND size is assumed unchanged
    since the last pass and is skipped, so re-running this costs one query
    per already-synced file rather than a duplicate upload. A file whose
    size did change (re-written since the last pass) gets a new attachment
    row, newest-wins -- the same convention `read_instruction_file` and
    `read_run_file` already use for a repeated filename.

    Never raises: a rejected file (oversized, disallowed content type) is
    logged and skipped, exactly as the design's Error Handling section
    requires -- a sync failure must never fail or block the run itself.

    Deliberately does not commit -- `db`'s tenant binding
    (`set_config('app.tenant_id', ..., true)`, see `oc8.db.session`) is
    transaction-local and a commit here would drop it for the rest of the
    caller's `tenant_session`/request, breaking whatever runs after this
    (the terminal `repo.transition` in `executor.py`, or the next run in
    `sync_active_run_outputs`'s per-tenant loop). Every caller already owns
    a `tenant_session`/`app_session` that commits on its own successful
    exit; this only needs to `flush()` (done inside `store_attachment_bytes`)
    so the dedup check above sees rows synced earlier in the same pass.

    Where the files come from (host walk, a tracked sandbox, or the last read
    taken when the runtime released it) is described in the module docstring.
    """
    files = await _collect_output(run_id, take_final=True)
    return await _store_output(db, tenant_id=tenant_id, run_id=run_id, files=files or [])


async def sync_final_run_output(
    db: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> list[m.FileAttachment]:
    """The executor's terminal sync: ``sync_run_output`` after releasing a
    sandbox the runtime left tracked, so a run that ends never leaves this
    process holding its sandbox handle. Same contract as ``sync_run_output``."""
    await release_run_sandbox(run_id)
    return await sync_run_output(db, tenant_id=tenant_id, run_id=run_id)


async def _store_output(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    files: list[tuple[str, bytes]],
) -> list[m.FileAttachment]:
    synced: list[m.FileAttachment] = []
    for rel_path, raw in files:
        existing = (
            await db.execute(
                select(m.FileAttachment.id)
                .where(
                    m.FileAttachment.tenant_id == tenant_id,
                    m.FileAttachment.owner_type == "agent_run",
                    m.FileAttachment.owner_id == run_id,
                    m.FileAttachment.filename == rel_path,
                    m.FileAttachment.size_bytes == len(raw),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        content_type = mimetypes.guess_type(rel_path)[0] or "application/octet-stream"
        try:
            row = await store_attachment_bytes(
                db,
                tenant_id=tenant_id,
                owner_type="agent_run",
                owner_id=run_id,
                filename=rel_path,
                raw=raw,
                content_type=content_type,
            )
        except AttachmentRejected as exc:
            logger.warning("run %s: skipped output file %r: %s", run_id, rel_path, exc)
            continue
        synced.append(row)
    return synced


async def sync_active_run_outputs() -> int:
    """The periodic half of the sync design: for every run still `running`,
    across every tenant, sync its `/workspace/output/` -- so a file becomes
    visible while the run is still in progress, not only once it finishes.
    Bounded by how many runs are concurrently running, not by total run
    history, which is what keeps this cheap at 100+ agent tenants (see the
    design's scalability requirement). Piggybacks on the run worker's
    existing 60s housekeeping timer (`runtime.worker.run_worker`) rather
    than a dedicated timer of its own -- one fewer clock in the process.

    Returns the number of files synced, for the caller to log.
    """
    from oc8.db.session import tenant_session
    from oc8.runtime.states import RunState
    from oc8.triggers.scheduler import list_active_tenant_ids

    _drop_stale_final_outputs()
    total = 0
    for tenant_id in await list_active_tenant_ids():
        async with tenant_session(tenant_id) as db:
            run_ids = (
                (
                    await db.execute(
                        select(m.AgentRun.id).where(
                            m.AgentRun.tenant_id == tenant_id,
                            m.AgentRun.state == RunState.RUNNING.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for run_id in run_ids:
                # The last read of a released sandbox belongs to the
                # executor's terminal sync, which runs next.
                files = await _collect_output(run_id, take_final=False)
                if files is None:
                    continue
                synced = await _store_output(db, tenant_id=tenant_id, run_id=run_id, files=files)
                total += len(synced)
    return total


def _drop_stale_final_outputs() -> None:
    cutoff = time.monotonic() - FINAL_OUTPUT_TTL_S
    for run_id, (taken, _files) in list(_final_outputs.items()):
        if taken < cutoff:
            logger.warning("run %s: dropped output nobody synced after the run", run_id)
            del _final_outputs[run_id]
