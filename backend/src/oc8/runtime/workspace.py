"""How a run's `/workspace/output/` becomes durable `FileAttachment` rows.

The isolated runtime and the CLI runtimes (`claude_code_runtime`,
`codex_runtime`, `opencode_runtime`) run with `/workspace` as a scratch dir
inside their sandbox, with no host directory behind it, and read their
output from the sandbox. A runtime that still bind-mounts a host directory
at `/workspace` must bind `f"{settings.runtime_session_root}/{run_id}"`
(`workspace_root`), the one path the host walk below reads.

`/workspace/output/` is the one reserved subdirectory an agent's produced
deliverables belong in; everything else under `/workspace` stays scratch
space, invisible to this sync and to the rest of oc8.

Two ways to read the output
---------------------------
* **Host walk.** A runtime whose sandbox bind-mounts `workspace_root(run_id)`
  is read by walking `output_dir(run_id)` on the host: `sync_run_output`,
  called by the executor when a leg ends (done, failed, interrupted,
  waiting for approval or for input). None of the runtimes in this
  repository uses it any more (Nanoclaw mounts its own session directory
  and hands files back through its own channel); it stays for a runtime
  that keeps mounts.
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
     files over the attachment cap, types attachments do not accept, and
     files refused earlier in the leg;
  3. fetches each remaining file on its own with `get_archive`, capped at
     its listed size plus `GROWTH_TOLERANCE_BYTES`, until the pass's budget
     (`run_output_pass_max_files`, `run_output_pass_max_bytes`) is used up;
     the next pass continues;
  4. accepts a fetch only if it is exactly one regular file with the
     requested basename, within that cap and, when the spec names a non-root
     user, owned by that uid. Every read (listing and fetch) runs as the
     sandbox's configured user (the driver contract), so a path the sandbox
     swaps for a link reaches nothing that user could not read anyway; the
     owner check is defence in depth, against the CONFIGURED uid, never one
     the sandbox reports. Content is unpacked with `safe_extract_to_memory`.

  Peak memory is therefore about twice one file (at most the attachment
  cap), not the whole directory. Nothing from the sandbox is ever written
  to the host.

  The sandbox outlives its command (oc8.sandbox.linger), so the last pass
  reads it through exec after the command has exited, on any driver.

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
import io
import logging
import os
import posixpath
import tarfile
import unicodedata
import uuid
from asyncio import to_thread
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.sandbox.archive import is_outside, safe_extract_to_memory
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.types import SandboxError, SandboxHandle, SandboxSpec
from oc8.storage.attachments import (
    ALLOWED_CONTENT_TYPES,
    MAX_ATTACHMENT_BYTES,
    AttachmentRejected,
    content_type_for,
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
#: How much a file may grow between listing and fetch and still be read.
GROWTH_TOLERANCE_BYTES = 4096
_LIST_TIMEOUT_S = 30.0

#: The periodic task's clock; replaced in tests.
_sleep = asyncio.sleep


def workspace_root(run_id: uuid.UUID) -> str:
    return os.path.join(get_settings().runtime_session_root, str(run_id))


def output_dir(run_id: uuid.UUID) -> str:
    return os.path.join(workspace_root(run_id), OUTPUT_SUBDIR)


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
    content_type = content_type_for(name)
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
    (the terminal `repo.transition` in `executor.py`). Every caller already owns
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
    """No output directory yet lists nothing. A userland without GNU ``find
    -printf`` or ``head -z`` (busybox) exits 3 instead of silently listing
    nothing; the images oc8 runs are Debian-based. Nothing in the listing is
    trusted beyond names and sizes: whose files they are comes from the
    sandbox's configuration (``run_output_sandbox``), never from here."""
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


@dataclass(frozen=True)
class _Listing:
    #: (name relative to /workspace/output, size) of each regular file.
    files: list[tuple[str, int]]


async def _list_sandbox_output(
    run_id: uuid.UUID, driver: SandboxDriver, handle: SandboxHandle
) -> _Listing:
    """The regular files in the sandbox's /workspace/output, at most
    ``run_output_max_files``. Malformed or unsafe entries are skipped."""
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
    files: list[tuple[str, int]] = []
    for record in records:
        size_s, sep, name = record.partition("\t")
        if not sep or not size_s.isdigit() or not name:
            logger.warning("run %s: skipped a malformed output listing entry", run_id)
            continue
        if is_outside(name) or posixpath.normpath(name) != name:
            logger.warning("run %s: skipped output path %r outside the directory", run_id, name)
            continue
        files.append((name, int(size_s)))
    return _Listing(files=files)


def _fetch_cap(listed: int) -> int:
    """Most file bytes one fetch accepts: the listed size plus a little
    growth, never more than the attachment limit."""
    return min(listed + GROWTH_TOLERANCE_BYTES, MAX_ATTACHMENT_BYTES)


async def _fetch_one(
    run_id: uuid.UUID,
    driver: SandboxDriver,
    handle: SandboxHandle,
    name: str,
    *,
    listed: int,
    owner_uid: int | None,
) -> bytes | None:
    """One output file's content, or None (logged) when it is not exactly
    the file that was listed.

    The fetch runs as the sandbox's own configured user (the driver
    contract), so a file or a directory above it swapped for a link between
    listing and fetch reaches nothing that user could not read anyway. On
    top of that, the archive must hold exactly one member: a regular file
    named like the requested basename, no larger than ``_fetch_cap(listed)``,
    and -- when ``owner_uid`` is known and not root -- owned by it.
    ``owner_uid`` comes from the sandbox's configuration, never from the
    sandbox. The read itself stops at that cap, and the content goes through
    ``safe_extract_to_memory``.
    """
    path = f"{OUTPUT_CONTAINER_PATH}/{name}"
    cap = _fetch_cap(listed)
    try:
        data = await driver.get_archive(handle, path, max_bytes=cap + _TAR_OVERHEAD)
        problem = _not_the_listed_file(data, posixpath.basename(name), cap=cap, owner_uid=owner_uid)
        if problem is not None:
            logger.warning("run %s: refused output file %r: %s", run_id, name, problem)
            return None
        files = safe_extract_to_memory(data, max_members=1, max_total_bytes=cap)
    except SandboxError as exc:
        logger.warning("run %s: skipped output file %r: %s", run_id, name, exc)
        return None
    raw = files.get(posixpath.basename(name))
    if raw is None or len(files) != 1:
        logger.warning("run %s: refused output file %r: not a regular file", run_id, name)
        return None
    return raw


def _not_the_listed_file(
    data: bytes, basename: str, *, cap: int, owner_uid: int | None
) -> str | None:
    """Why ``data`` is not one regular file ``basename`` of at most ``cap``
    bytes owned by ``owner_uid`` (any owner when it is None or 0), or None.
    Reads the headers only; it never stops at more than two members."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            first = tar.next()
            if first is None:
                return "empty archive"
            if tar.next() is not None:
                return "more than one member"
    except tarfile.TarError as exc:
        return f"not a plain tar: {exc}"
    if not first.isfile():
        return "not a regular file"
    if first.name != basename:
        return f"archive member is {first.name!r}"
    if first.size > cap:
        return f"{first.size} bytes is more than the {cap} expected"
    if owner_uid not in (None, 0) and first.uid != owner_uid:
        return f"owned by uid {first.uid}, not the sandbox user {owner_uid}"
    return None


async def sync_sandbox_output(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    driver: SandboxDriver,
    handle: SandboxHandle,
    refused: set[tuple[str, int]] | None = None,
    owner_uid: int | None = None,
) -> list[m.FileAttachment]:
    """One pass over a live sandbox's /workspace/output: list, skip what is
    stored, fetch and store the rest one file at a time (module docstring).

    A pass fetches at most ``run_output_pass_max_files`` files and
    ``run_output_pass_max_bytes`` of listed size, then stops; the next pass
    continues, because what this one stored is skipped. ``refused`` (kept
    across the passes of one leg by ``run_output_sandbox``) collects files
    refused at fetch, so they are not fetched again at the same size.

    Never raises: a failed listing syncs nothing, a failed file is skipped.
    Does not commit, like `sync_run_output`.
    """
    settings = get_settings()
    refused = refused if refused is not None else set()
    try:
        listing = await _list_sandbox_output(run_id, driver, handle)
    except SandboxError as exc:
        logger.warning("run %s: could not list output in the sandbox: %s", run_id, exc)
        return []
    except Exception:
        logger.exception("run %s: listing output in the sandbox failed", run_id)
        return []
    if not listing.files:
        return []
    seen = await _existing(db, tenant_id=tenant_id, run_id=run_id)
    synced: list[m.FileAttachment] = []
    names: dict[str, str] = {}
    files_left = settings.run_output_pass_max_files
    bytes_left = settings.run_output_pass_max_bytes
    for rel_path, size in listing.files:
        name = sanitize_output_name(rel_path)
        if name is None:
            continue
        if names.setdefault(name, rel_path) != rel_path:
            logger.warning(
                "run %s: output files %r and %r are stored under the same name %r",
                run_id,
                names[name],
                rel_path,
                name,
            )
        if (name, size) in seen or (rel_path, size) in refused:
            continue
        if size > MAX_ATTACHMENT_BYTES:
            logger.warning(
                "run %s: skipped output file %r: %d bytes is over the attachment limit",
                run_id,
                name,
                size,
            )
            continue
        content_type = content_type_for(name)
        if content_type not in ALLOWED_CONTENT_TYPES:
            logger.info("run %s: skipped output file %r: type %s", run_id, name, content_type)
            continue
        cap = _fetch_cap(size)
        if files_left <= 0 or cap > bytes_left:
            logger.info("run %s: output pass budget used up, continuing next pass", run_id)
            break
        files_left -= 1
        bytes_left -= cap
        try:
            raw = await _fetch_one(
                run_id, driver, handle, rel_path, listed=size, owner_uid=owner_uid
            )
        except Exception:
            logger.exception("run %s: could not fetch output file %r", run_id, name)
            raw = None
        if raw is None:
            refused.add((rel_path, size))
            continue
        if (name, len(raw)) in seen:
            continue
        row = await _store_one(db, tenant_id=tenant_id, run_id=run_id, name=name, raw=raw)
        if row is not None:
            seen.add((name, len(raw)))
            synced.append(row)
    return synced


def configured_uid(spec: SandboxSpec) -> int | None:
    """The uid the sandbox was configured to run as (``spec.user``), or None
    for the image's own user. Never asked of the sandbox."""
    if not spec.user:
        return None
    uid = spec.user.partition(":")[0]
    return int(uid) if uid.isdigit() else None


async def _sync_sandbox_in_own_session(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    driver: SandboxDriver,
    handle: SandboxHandle,
    refused: set[tuple[str, int]],
    owner_uid: int | None,
) -> int:
    from oc8.db.session import tenant_session

    async with tenant_session(tenant_id) as db:
        synced = await sync_sandbox_output(
            db,
            tenant_id=tenant_id,
            run_id=run_id,
            driver=driver,
            handle=handle,
            refused=refused,
            owner_uid=owner_uid,
        )
    return len(synced)


async def _sync_periodically(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    driver: SandboxDriver,
    handle: SandboxHandle,
    refused: set[tuple[str, int]],
    owner_uid: int | None,
) -> None:
    interval = get_settings().run_output_sync_interval_s
    while True:
        await _sleep(interval)
        try:
            await _sync_sandbox_in_own_session(
                run_id, tenant_id, driver, handle, refused, owner_uid
            )
        except Exception:
            logger.exception("run %s: periodic output sync failed", run_id)


async def _last_pass(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    driver: SandboxDriver,
    handle: SandboxHandle,
    refused: set[tuple[str, int]],
    owner_uid: int | None,
) -> None:
    try:
        async with asyncio.timeout(get_settings().run_output_final_read_timeout_s):
            await _sync_sandbox_in_own_session(
                run_id, tenant_id, driver, handle, refused, owner_uid
            )
    except TimeoutError:
        logger.warning("run %s: the last output sync timed out", run_id)
    except Exception:
        logger.exception("run %s: the last output sync failed", run_id)


#: Last passes still running after their leg was cancelled; held so they are
#: not garbage-collected mid-flight.
_detached_passes: set[asyncio.Task[None]] = set()


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

    The last pass is shielded and bounded by
    ``run_output_final_read_timeout_s``: if the block's task is cancelled
    again while it runs, the pass still finishes (the cancellation then
    propagates at once, so the caller's teardown may race the pass; a pass
    against a sandbox already gone just logs).
    """
    if spec.mounts:
        yield
        return
    refused: set[tuple[str, int]] = set()
    owner_uid = configured_uid(spec)
    task = asyncio.create_task(
        _sync_periodically(run_id, tenant_id, driver, handle, refused, owner_uid),
        name=f"output-sync-{run_id}",
    )
    try:
        yield
    finally:
        task.cancel()
        # Waits without re-raising the task's CancelledError; a cancellation
        # of THIS task still propagates.
        await asyncio.wait([task])
        last = asyncio.create_task(
            _last_pass(run_id, tenant_id, driver, handle, refused, owner_uid),
            name=f"output-sync-last-{run_id}",
        )
        _detached_passes.add(last)
        last.add_done_callback(_detached_passes.discard)
        await asyncio.shield(last)
