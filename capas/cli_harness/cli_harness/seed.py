"""What a CLI-harness sandbox starts with, and what it keeps between legs.

A CLI runtime's sandbox has no host directory behind it. It gets:

* **scratch dirs** for ``/workspace`` and for the CLI's own home, which the
  driver provides empty and writable (a volume on Docker, an ``emptyDir`` on
  Kubernetes);
* **a seed**, one tar unpacked at ``/`` before the command starts, carrying
  the CLI's config files (one of which holds the run's bearer token) and, on
  a resume leg, the CLI home the previous leg saved. Because the files are in
  place before the CLI's first instruction, no CLI can start under its
  default config, which a write after start could not guarantee (the Codex
  race described in codex_runtime/runtime/runtime.py).

At the end of every leg, the runtime reads the CLI home back with
``get_archive`` and keeps it in ``RunStateStore`` (object storage, keyed by
tenant and run), so the next leg can resume on any worker. That transcript
lives outside ``/workspace`` for every CLI: Claude Code under
``$CLAUDE_CONFIG_DIR``, Codex under ``$CODEX_HOME/sessions``, opencode under
``$XDG_DATA_HOME/opencode``.

Ownership: every seed entry belongs to ``sandbox_owner()``, the uid the
sandbox runs as (``settings.sandbox_user``, else the CLI images' own
uid-1000 ``node`` user). Directory entries start at each seeded dir itself,
never at its ancestors, so ``/home/node`` keeps the image's owner. Every
seeded dir is also one of the sandbox's scratch dirs, so it exists and is
writable before the seed lands on any driver, including one that unpacks
the seed as the sandbox user rather than as root.

A config file in the seed is writable by the agent for the rest of its leg
(no read-only mounts any more), but it is never part of saved state: every
leg seeds it fresh, so an edit dies with its sandbox.

Saved homes lose symlinks, file modes and mtimes (see ``oc8.runtime.
run_state``). That is why every runtime resumes by an explicit session id,
never by "the most recent session".
"""

from __future__ import annotations

import io
import logging
import posixpath
import tarfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from oc8.config import get_settings
from oc8.runtime.run_state import RunStateStore, pack_files
from oc8.sandbox.archive import safe_extract_to_memory
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.types import SandboxHandle, ScratchDir

logger = logging.getLogger(__name__)

#: The CLI images' HOME, the home of their own uid-1000 ``node`` user.
CONTAINER_HOME = "/home/node"
WORKSPACE = "/workspace"
#: The uid:gid of ``node`` in the CLI images (node:22-bookworm-slim).
IMAGE_UID = 1000

#: How long a CLI that was asked to exit gets before its home is read anyway.
STOP_GRACE_S = 10.0

WORKSPACE_SCRATCH_MB = 4096
HOME_SCRATCH_MB = 1024
CONFIG_SCRATCH_MB = 64


def workspace_scratch() -> ScratchDir:
    return ScratchDir(WORKSPACE, WORKSPACE_SCRATCH_MB)


def sandbox_owner() -> tuple[int, int]:
    """The uid:gid the sandbox runs as, and so the owner of its seed."""
    user = get_settings().sandbox_user
    if not user:
        return IMAGE_UID, IMAGE_UID
    # Shape guaranteed by Settings._validate_sandbox_user.
    uid_s, _, gid_s = user.partition(":")
    uid = int(uid_s)
    return uid, int(gid_s) if gid_s else uid


@dataclass(frozen=True)
class SeedDir:
    """An absolute in-sandbox directory and the files (relative name to
    content) the seed puts in it. Empty ``files`` still seeds the directory
    itself, owned by the sandbox user."""

    path: str
    files: Mapping[str, bytes] = field(default_factory=dict)


def build_seed(dirs: Iterable[SeedDir], *, uid: int, gid: int) -> bytes:
    """One tar of every dir, for ``SandboxSpec.seed``. Files are 0600, dirs
    0700, all owned by ``uid:gid``."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as merged:
        for seed_dir in dirs:
            if not posixpath.isabs(seed_dir.path):
                raise ValueError(f"seed dir must be absolute: {seed_dir.path!r}")
            part = pack_files(seed_dir.files, prefix=seed_dir.path, uid=uid, gid=gid)
            with tarfile.open(fileobj=io.BytesIO(part), mode="r:") as src:
                for member in src.getmembers():
                    merged.addfile(member, src.extractfile(member) if member.isfile() else None)
    return out.getvalue()


async def load_home(
    *, tenant_id: uuid.UUID, run_id: uuid.UUID, name: str
) -> dict[str, bytes] | None:
    """The home a previous leg saved, or None. Raises when the stored state
    is refused or storage fails: a resume leg without its transcript cannot
    resume."""
    return await RunStateStore(tenant_id).load_files(run_id, name)


async def save_home(
    driver: SandboxDriver,
    handle: SandboxHandle,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    name: str,
    path: str,
    exclude: Callable[[str], bool] | None = None,
) -> bool:
    """Read ``path`` out of the sandbox and keep it as this run's ``name``
    state, without the files ``exclude`` names (relative to ``path``).

    Never raises: a leg's outcome is already decided when this runs, and the
    sandbox may be gone. A failure is logged; the next leg then finds the
    state an earlier leg saved, or none, and a runtime refuses to resume
    without any. Returns whether the state was saved.

    ``tenant_id`` must be the run's tenant as loaded under RLS, never a value
    the sandbox supplied (see ``RunStateStore``).
    """
    settings = get_settings()
    try:
        tar = await driver.get_archive(handle, path, max_bytes=settings.run_state_max_bytes)
        if exclude is not None:
            files = safe_extract_to_memory(
                tar,
                max_members=settings.run_state_max_files,
                max_total_bytes=settings.run_state_max_bytes,
                on_link="skip",
            )
            tar = pack_files({rel: data for rel, data in files.items() if not exclude(rel)})
        await RunStateStore(tenant_id).save(run_id, name, tar)
    except Exception:
        logger.exception("run %s: could not save %s from %s", run_id, name, path)
        return False
    return True


async def stop_cli(driver: SandboxDriver, handle: SandboxHandle) -> bool:
    """Ask a CLI that is still running (a parked, cancelled or timed-out leg)
    to exit, and wait for it, so its home is read at rest: opencode's SQLite
    database with its ``-wal`` file, a transcript the CLI appends to. The
    old bind mounts got the same from teardown, which killed the CLI before
    the next leg read the host directory.

    SIGTERM goes to PID 1, the images' tini, which passes it on to the CLI.
    Returns whether the CLI exited within ``STOP_GRACE_S``; if not, the home
    is read while it runs, which is logged.
    """
    try:
        await driver.exec(handle, ["kill", "-TERM", "1"], timeout=STOP_GRACE_S)
    except Exception:
        logger.debug("could not signal the sandbox's init process", exc_info=True)
    try:
        await driver.wait(handle, timeout_s=STOP_GRACE_S)
    except Exception:
        logger.warning(
            "sandbox %s did not exit within %.0fs of SIGTERM; reading its home while it runs",
            handle.container_id[:12],
            STOP_GRACE_S,
        )
        return False
    return True
