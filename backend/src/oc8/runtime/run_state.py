"""State a run keeps between legs, in object storage.

A runtime without host mounts loses everything in its sandbox when the sandbox
is torn down. What leg 2 needs from leg 1 (a CLI's home with its transcript,
for instance) is read out with ``SandboxDriver.get_archive`` at the end of a
leg, saved here, and loaded again to seed the next leg's sandbox, on whatever
worker picks it up.

Keys are ``{tenant_id}/runs/{run_id}/state/{name}.tar``. They start with the
tenant id like every other object of a tenant (see
``oc8.storage.attachments``), so deleting a tenant's prefix deletes its run
state too.

The tar a sandbox hands back is untrusted. ``save`` reads it through
``safe_extract_to_memory`` and stores a tar rebuilt from the regular files
alone; ``load`` checks the stored object the same way again before anything
goes back into a sandbox. On save, symlinks, hardlinks and special files are
skipped (and counted in the log), so one link in a CLI home does not cost the
transcript. Names that leave the root and anything over
``run_state_max_files`` / ``run_state_max_bytes`` are refused with
``RunStateRejected``. A rebuilt tar keeps names and content only: no links, no
file modes beyond ``file_mode``/``dir_mode``, no original mtimes.

Peak memory of a save or load is about twice the archive size: the tar plus
the files read from it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import posixpath
import re
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from typing import IO

from oc8.config import get_settings
from oc8.sandbox.archive import is_outside, safe_extract_to_memory
from oc8.sandbox.types import SandboxError
from oc8.storage import s3

logger = logging.getLogger(__name__)

TAR_CONTENT_TYPE = "application/x-tar"

#: Above this, a filtered state tar is spooled to disk rather than memory.
_SPOOL_IN_MEMORY_BYTES = 8 * 1024 * 1024

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class RunStateRejected(Exception):
    """A state archive that is unsafe, too large or has too many members."""


def pack_files(
    files: Mapping[str, bytes],
    *,
    prefix: str = "",
    uid: int = 0,
    gid: int = 0,
    file_mode: int = 0o600,
    dir_mode: int = 0o700,
) -> bytes:
    """A plain tar of ``files`` (relative name to content), each under
    ``prefix``, owned by ``uid``:``gid``.

    Directory entries are emitted for the prefix's LAST component and
    everything below it, before the files in them and with the same owner, so
    a non-root process in the sandbox can add files next to the restored ones.
    Ancestors of the prefix (``home``, ``home/agent``) get no entry: they
    exist in the image and keep its owner and mode. Use it to put loaded state
    into a seed, which is unpacked at ``/``:
    ``pack_files(files, prefix="home/agent/.codex", ...)``.
    """
    root = posixpath.normpath(prefix.strip("/")) if prefix.strip("/") else ""
    if root and is_outside(root):
        raise ValueError(f"prefix leaves its root: {prefix!r}")
    now = int(time.time())
    seen: set[str] = set()
    buf = io.BytesIO()

    # The first path component that gets an entry: the prefix's last one.
    first = len(root.split("/")) if root else 1

    def add_dir(name: str, tar: tarfile.TarFile) -> None:
        parts = name.split("/")
        for i in range(first, len(parts) + 1):
            path = "/".join(parts[:i])
            if path in seen:
                continue
            seen.add(path)
            info = tarfile.TarInfo(path)
            info.type = tarfile.DIRTYPE
            info.mode, info.uid, info.gid, info.mtime = dir_mode, uid, gid, now
            tar.addfile(info)

    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        if root:
            add_dir(root, tar)
        for rel in sorted(files):
            if not rel or is_outside(rel):
                raise ValueError(f"file name leaves its root: {rel!r}")
            name = posixpath.normpath(posixpath.join(root, rel))
            parent = posixpath.dirname(name)
            if parent:
                add_dir(parent, tar)
            data = files[rel]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode, info.uid, info.gid, info.mtime = file_mode, uid, gid, now
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _checked_files(
    tar: bytes, *, skip_links: bool = False, max_bytes: int | None = None
) -> dict[str, bytes]:
    settings = get_settings()
    limit = max_bytes if max_bytes is not None else settings.run_state_max_bytes
    if len(tar) > limit:
        raise RunStateRejected(f"state archive is {len(tar)} bytes, over the {limit} limit")
    skipped: list[str] = []
    try:
        files = safe_extract_to_memory(
            tar,
            max_members=settings.run_state_max_files,
            max_total_bytes=limit,
            on_link="skip" if skip_links else "refuse",
            skipped=skipped,
        )
    except SandboxError as exc:
        raise RunStateRejected(str(exc)) from exc
    if skipped:
        logger.info(
            "run state: skipped %d link(s) or special file(s), first %r",
            len(skipped),
            skipped[0],
        )
    return files


class RunStateStore:
    """Named state archives of one tenant's runs.

    ``save(run_id, name, tar)`` stores a tar as read with ``get_archive``
    (names relative to the saved directory) and replaces any earlier one of the
    same name. ``load(run_id, name)`` returns it as a tar ready for
    ``put_archive`` into that directory, or None when nothing was saved.
    ``load_files`` returns the files themselves, for building a seed with
    ``pack_files``.

    ``tenant_id`` must be the tenant of the run as loaded under RLS (the
    ``agent_run`` row's ``tenant_id`` read in a ``tenant_session``), never a
    value taken from the sandbox, a request body or a token claim alone: the
    key prefix is the only thing separating one tenant's state from another's.
    """

    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id

    @staticmethod
    def key(tenant_id: uuid.UUID, run_id: uuid.UUID, name: str) -> str:
        if not _NAME.fullmatch(name):
            raise ValueError(f"invalid state name {name!r}")
        return f"{tenant_id}/runs/{run_id}/state/{name}.tar"

    async def save(
        self, run_id: uuid.UUID, name: str, tar: bytes, *, max_bytes: int | None = None
    ) -> None:
        """``max_bytes`` overrides ``run_state_max_bytes`` for this state (the
        workspace has its own cap)."""
        key = self.key(self.tenant_id, run_id, name)
        files = _checked_files(tar, skip_links=True, max_bytes=max_bytes)
        await s3.put_object(key, pack_files(files), TAR_CONTENT_TYPE)

    async def load_files(
        self, run_id: uuid.UUID, name: str, *, max_bytes: int | None = None
    ) -> dict[str, bytes] | None:
        key = self.key(self.tenant_id, run_id, name)
        try:
            raw = await s3.get_object(key)
        except s3.ObjectNotFound:
            return None
        return _checked_files(raw, max_bytes=max_bytes)

    async def load(
        self, run_id: uuid.UUID, name: str, *, uid: int = 0, gid: int = 0
    ) -> bytes | None:
        files = await self.load_files(run_id, name)
        if files is None:
            return None
        return pack_files(files, uid=uid, gid=gid)

    async def delete(self, run_id: uuid.UUID, name: str) -> None:
        """Remove one state; nothing saved is not an error."""
        await s3.delete_object(self.key(self.tenant_id, run_id, name))


def state_prefix(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"{tenant_id}/runs/{run_id}/state/"


async def delete_run_state(tenant_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Delete every state object of a run. Best-effort: logged, never raised.

    Called once the run has ended (done, failed or interrupted): nothing
    resumes it any more, and a CLI home or workspace should not outlive it.
    A run that is waiting for approval or input keeps its state.
    """
    try:
        await s3.delete_prefix(state_prefix(tenant_id, run_id))
    except Exception:
        logger.warning("run %s: could not delete its saved state", run_id, exc_info=True)


#: Deletions scheduled at a terminal transition, kept until they finish.
_pending_deletions: set[asyncio.Task[None]] = set()


def delete_run_state_soon(tenant_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Schedule ``delete_run_state`` without waiting for it."""
    task = asyncio.get_running_loop().create_task(
        delete_run_state(tenant_id, run_id), name=f"delete-run-state-{run_id}"
    )
    _pending_deletions.add(task)
    task.add_done_callback(_pending_deletions.discard)


async def drain_deletions() -> None:
    """Wait for every scheduled deletion (tests and shutdown)."""
    while _pending_deletions:
        await asyncio.gather(*list(_pending_deletions), return_exceptions=True)


def filter_tar(tar: bytes, exclude: Callable[[str], bool]) -> bytes:
    """``tar`` without the files ``exclude`` names (by normalised relative
    name), and without links and special files.

    Streams one member at a time into a spooled file, never extracting the
    whole archive. The result is still untrusted: ``RunStateStore.save``
    checks it again.
    """
    out: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY_BYTES)
    with out:
        with (
            tarfile.open(fileobj=io.BytesIO(tar), mode="r|") as src,
            tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as dst,
        ):
            for member in src:
                if not (member.isfile() or member.isdir()):
                    continue
                rel = posixpath.normpath(member.name)
                if exclude(rel):
                    continue
                dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        out.seek(0)
        return out.read()
