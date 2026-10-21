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
  `output_dir(run_id)` on the host: `sync_run_output`, called by the
  executor when a leg ends (done, failed, interrupted, waiting for approval
  or for input) and by the worker's housekeeping (`sync_active_run_outputs`).
* **From the sandbox.** A sandbox without mounts has no host directory. The
  runtime wraps the leg in `run_output_sandbox(run_id, tenant_id, spec,
  driver, handle)`. That starts a per-run task which stores the output every
  `run_output_sync_interval_s` in its own tenant session, and on exit (normal,
  exception or cancellation) stops the task and takes one last pass, bounded
  by `run_output_final_read_timeout_s`, before the runtime tears the sandbox
  down. Each pass (`sync_sandbox_output`):

  1. lists `/workspace/output` with GNU `find -printf '%s\\t%P\\0'`, cut
     to `run_output_max_files` entries;
  2. skips files already stored under the same (sanitised) name and size,
     and files over the attachment cap, one at a time;
  3. fetches each remaining file on its own with `get_archive`, capped at
     the attachment limit, and unpacks it with `safe_extract_to_memory`.

  Peak memory is therefore about twice one file (at most the attachment
  cap), not the whole directory. Everything from the sandbox is untrusted:
  names that leave the directory, links and special files are skipped, and
  nothing is ever written to the host.

  The task is the run's own because the worker's housekeeping cannot run
  while that worker is executing a run.

Names are sanitised before they are stored (`sanitize_output_name`): control
and format characters are removed, each segment is capped at 200 bytes and
the whole name at 255. One
file that fails to store is logged and skipped inside a savepoint, so it can
never fail the run or break the caller's session.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import posixpath
import unicodedata
import uuid
from asyncio import to_thread
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.sandbox.archive import is_outside, safe_extract_to_memory
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.types import SandboxError, SandboxHandle, SandboxSpec
from oc8.storage.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentRejected,
    store_attachment_bytes,
)

logger = logging.getLogger(__name__)

OUTPUT_SUBDIR = "output"
OUTPUT_CONTAINER_PATH = "/workspace/output"

#: Longest stored output name, and longest segment of it, in UTF-8 bytes.
MAX_OUTPUT_NAME_BYTES = 255
MAX_OUTPUT_SEGMENT_BYTES = 200
#: Room for tar headers (PAX for long names) and padding on top of a file.
_TAR_OVERHEAD = 64 * 1024
_LIST_TIMEOUT_S = 30.0

#: The periodic task's clock; replaced in tests.
_sleep = asyncio.sleep


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


def _cap(segment: str, limit: int, *, keep_ext: bool) -> str:
    if len(segment.encode()) <= limit:
        return segment
    stem, ext = posixpath.splitext(segment) if keep_ext else (segment, "")
    if len(ext.encode()) > 32:
        stem, ext = segment, ""
    return stem.encode()[: limit - len(ext.encode())].decode(errors="ignore") + ext


def sanitize_output_name(name: str) -> str | None:
    """``name`` as it is stored: control and format characters removed from
    every path segment; empty, ``.`` and ``..`` segments dropped; each segment
    capped at ``MAX_OUTPUT_SEGMENT_BYTES`` (the storage key puts a 37-byte
    uuid in front of the first one, and object stores refuse a key segment
    over 255 bytes); the whole name capped at ``MAX_OUTPUT_NAME_BYTES`` by
    dropping leading directories. A short extension is kept. None when
    nothing is left."""
    segments = []
    for segment in name.split("/"):
        clean = "".join(ch for ch in segment if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
        if clean and clean not in (".", ".."):
            segments.append(clean)
    if not segments:
        return None
    capped = [_cap(seg, MAX_OUTPUT_SEGMENT_BYTES, keep_ext=False) for seg in segments[:-1]]
    capped.append(_cap(segments[-1], MAX_OUTPUT_SEGMENT_BYTES, keep_ext=True))
    while len(capped) > 1 and len("/".join(capped).encode()) > MAX_OUTPUT_NAME_BYTES:
        capped.pop(0)
    return "/".join(capped)


async def _existing(
    db: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> set[tuple[str, int]]:
    rows = await db.execute(
        select(m.FileAttachment.filename, m.FileAttachment.size_bytes).where(
            m.FileAttachment.tenant_id == tenant_id,
            m.FileAttachment.owner_type == "agent_run",
            m.FileAttachment.owner_id == run_id,
        )
    )
    return {(name, size) for name, size in rows.all()}


async def _store_one(
    db: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID, name: str, raw: bytes
) -> m.FileAttachment | None:
    """Store one output file inside a savepoint. Never raises: a rejected or
    failing file is logged and skipped, and the savepoint keeps the caller's
    transaction (and its tenant binding) usable."""
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    try:
        async with db.begin_nested():
            return await store_attachment_bytes(
                db,
                tenant_id=tenant_id,
                owner_type="agent_run",
                owner_id=run_id,
                filename=name,
                raw=raw,
                content_type=content_type,
            )
    except AttachmentRejected as exc:
        logger.warning("run %s: skipped output file %r: %s", run_id, name, exc)
    except Exception:
        logger.exception("run %s: could not store output file %r", run_id, name)
    return None


async def sync_run_output(
    db: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> list[m.FileAttachment]:
    """Upload every new/changed file under this run's host
    `output_dir(run_id)` into `FileAttachment` (`owner_type="agent_run"`,
    `owner_id=run_id`). This is the host walk; a sandbox without mounts is
    synced by `run_output_sandbox` instead, and this finds nothing for it.

    Safe to call repeatedly against the same run -- both from the terminal
    transition and from the periodic active-run pass: a file already attached
    under the same filename AND size is assumed unchanged and is skipped. A
    file whose size did change gets a new attachment row, newest-wins -- the
    same convention `read_instruction_file` and `read_run_file` already use
    for a repeated filename.

    Never raises for a file: a rejected or failing file is logged and
    skipped -- a sync failure must never fail or block the run itself.

    Deliberately does not commit -- `db`'s tenant binding
    (`set_config('app.tenant_id', ..., true)`, see `oc8.db.session`) is
    transaction-local and a commit here would drop it for the rest of the
    caller's `tenant_session`/request, breaking whatever runs after this
    (the terminal `repo.transition` in `executor.py`, or the next run in
    `sync_active_run_outputs`'s per-tenant loop). Every caller already owns
    a session that commits on its own successful exit; each file is flushed
    inside a savepoint, so the dedup sees rows synced earlier in the pass.
    """
    files = await to_thread(_scan_output_dir, output_dir(run_id))
    if not files:
        return []
    seen = await _existing(db, tenant_id=tenant_id, run_id=run_id)
    synced: list[m.FileAttachment] = []
    for rel_path, raw in files:
        name = sanitize_output_name(rel_path)
        if name is None or (name, len(raw)) in seen:
            continue
        row = await _store_one(db, tenant_id=tenant_id, run_id=run_id, name=name, raw=raw)
        if row is not None:
            seen.add((name, len(raw)))
            synced.append(row)
    return synced


def _list_command(limit: int) -> list[str]:
    """No output directory yet lists nothing. A userland without GNU
    ``find -printf`` or ``head -z`` (busybox) exits 3 instead of silently
    listing nothing; the images oc8 runs are Debian-based."""
    out = OUTPUT_CONTAINER_PATH
    return [
        "sh",
        "-c",
        f"[ -d {out} ] || exit 0; "
        f"find {out} -maxdepth 0 -printf '' 2>/dev/null && head -z -n 1 </dev/null 2>/dev/null"
        " || exit 3; "
        f"find {out} -mindepth 1 -type f -printf '%s\\t%P\\0' 2>/dev/null"
        f" | head -z -n {limit}",
    ]


async def _list_sandbox_output(
    run_id: uuid.UUID, driver: SandboxDriver, handle: SandboxHandle
) -> list[tuple[str, int]]:
    """(name relative to /workspace/output, size) of each regular file, at
    most ``run_output_max_files``. Malformed or unsafe entries are skipped."""
    limit = get_settings().run_output_max_files
    result = await driver.exec(handle, _list_command(limit + 1), timeout=_LIST_TIMEOUT_S)
    if result.exit_code != 0:
        raise SandboxError(
            f"listing /workspace/output exited {result.exit_code}"
            + (" (needs GNU find and head)" if result.exit_code == 3 else "")
        )
    records = [r for r in result.output.split("\0") if r]
    if len(records) > limit:
        logger.warning(
            "run %s: more than %d output files, syncing the first %d", run_id, limit, limit
        )
        records = records[:limit]
    entries: list[tuple[str, int]] = []
    for record in records:
        size_s, sep, name = record.partition("\t")
        if not sep or not size_s.isdigit() or not name:
            logger.warning("run %s: skipped a malformed output listing entry", run_id)
            continue
        if is_outside(name) or posixpath.normpath(name) != name:
            logger.warning("run %s: skipped output path %r outside the directory", run_id, name)
            continue
        entries.append((name, int(size_s)))
    return entries


async def _fetch_one(
    run_id: uuid.UUID, driver: SandboxDriver, handle: SandboxHandle, name: str
) -> bytes | None:
    """One output file's content, through ``safe_extract_to_memory``. None
    (logged) when it is gone, too large, a link or otherwise unreadable."""
    path = f"{OUTPUT_CONTAINER_PATH}/{name}"
    try:
        data = await driver.get_archive(
            handle, path, max_bytes=MAX_ATTACHMENT_BYTES + _TAR_OVERHEAD
        )
        files = safe_extract_to_memory(data, max_members=1, max_total_bytes=MAX_ATTACHMENT_BYTES)
    except SandboxError as exc:
        logger.warning("run %s: skipped output file %r: %s", run_id, name, exc)
        return None
    raw = files.get(posixpath.basename(name))
    if raw is None or len(files) != 1:
        logger.warning("run %s: skipped output file %r: not a regular file", run_id, name)
        return None
    return raw


async def sync_sandbox_output(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    driver: SandboxDriver,
    handle: SandboxHandle,
) -> list[m.FileAttachment]:
    """One pass over a live sandbox's /workspace/output: list, skip what is
    stored, fetch and store the rest one file at a time (module docstring).

    Never raises: a failed listing syncs nothing, a failed file is skipped.
    Does not commit, like `sync_run_output`.
    """
    try:
        entries = await _list_sandbox_output(run_id, driver, handle)
    except SandboxError as exc:
        logger.warning("run %s: could not list output in the sandbox: %s", run_id, exc)
        return []
    except Exception:
        logger.exception("run %s: listing output in the sandbox failed", run_id)
        return []
    if not entries:
        return []
    seen = await _existing(db, tenant_id=tenant_id, run_id=run_id)
    synced: list[m.FileAttachment] = []
    for rel_path, size in entries:
        name = sanitize_output_name(rel_path)
        if name is None or (name, size) in seen:
            continue
        if size > MAX_ATTACHMENT_BYTES:
            logger.warning(
                "run %s: skipped output file %r: %d bytes is over the attachment limit",
                run_id,
                name,
                size,
            )
            continue
        try:
            raw = await _fetch_one(run_id, driver, handle, rel_path)
        except Exception:
            logger.exception("run %s: could not fetch output file %r", run_id, name)
            continue
        if raw is None or (name, len(raw)) in seen:
            continue
        row = await _store_one(db, tenant_id=tenant_id, run_id=run_id, name=name, raw=raw)
        if row is not None:
            seen.add((name, len(raw)))
            synced.append(row)
    return synced


async def _sync_sandbox_in_own_session(
    run_id: uuid.UUID, tenant_id: uuid.UUID, driver: SandboxDriver, handle: SandboxHandle
) -> int:
    from oc8.db.session import tenant_session

    async with tenant_session(tenant_id) as db:
        synced = await sync_sandbox_output(
            db, tenant_id=tenant_id, run_id=run_id, driver=driver, handle=handle
        )
    return len(synced)


async def _sync_periodically(
    run_id: uuid.UUID, tenant_id: uuid.UUID, driver: SandboxDriver, handle: SandboxHandle
) -> None:
    interval = get_settings().run_output_sync_interval_s
    while True:
        await _sleep(interval)
        try:
            await _sync_sandbox_in_own_session(run_id, tenant_id, driver, handle)
        except Exception:
            logger.exception("run %s: periodic output sync failed", run_id)


@asynccontextmanager
async def run_output_sandbox(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    spec: SandboxSpec,
    driver: SandboxDriver,
    handle: SandboxHandle,
) -> AsyncIterator[None]:
    """Keep this run's /workspace/output stored while the block runs, and
    take the last pass when it ends. Tear the sandbox down only AFTER the
    block::

        handle = await driver.provision(spec)
        try:
            async with run_output_sandbox(run_id, tenant_id, spec, driver, handle):
                ...  # run the leg
        finally:
            await driver.teardown(handle)

    A spec with mounts does nothing here: its output is on the host and the
    executor's `sync_run_output` reads it. Each pass uses its own
    `tenant_session` and commits, so stored output survives the worker dying
    afterwards. ``tenant_id`` must be the run's tenant as loaded under RLS
    (the ``agent_run`` row), never a value the sandbox supplied.
    """
    if spec.mounts:
        yield
        return
    task = asyncio.create_task(
        _sync_periodically(run_id, tenant_id, driver, handle), name=f"output-sync-{run_id}"
    )
    try:
        yield
    finally:
        task.cancel()
        # Waits without re-raising the task's CancelledError; a cancellation
        # of THIS task still propagates.
        await asyncio.wait([task])
        try:
            async with asyncio.timeout(get_settings().run_output_final_read_timeout_s):
                await _sync_sandbox_in_own_session(run_id, tenant_id, driver, handle)
        except TimeoutError:
            logger.warning("run %s: the last output sync timed out", run_id)
        except Exception:
            logger.exception("run %s: the last output sync failed", run_id)


async def sync_active_run_outputs() -> int:
    """The periodic host walk: for every run still `running`, across every
    tenant, sync its host `output_dir` -- so a file becomes visible while the
    run is still in progress. Runs on the run worker's housekeeping timer
    (`runtime.worker.run_worker`), which fires only between runs that worker
    executes. A sandbox without mounts is synced by its own task instead
    (`run_output_sandbox`); this finds nothing for it.

    Returns the number of files synced, for the caller to log.
    """
    from oc8.db.session import tenant_session
    from oc8.runtime.states import RunState
    from oc8.triggers.scheduler import list_active_tenant_ids

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
                synced = await sync_run_output(db, tenant_id=tenant_id, run_id=run_id)
                total += len(synced)
    return total
