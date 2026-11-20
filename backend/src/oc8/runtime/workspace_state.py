"""A run's ``/workspace`` kept between legs of a sandbox without mounts.

Teardown destroys the sandbox at every park, so whatever the agent left in
``/workspace`` (a checkout, notes, half-done work) would be gone on the
resume leg. At the end of each leg the runtime saves it here, as the run's
``workspace`` state, and the next leg's seed puts it back before the command
starts. ``output/`` is left out: it is stored as attachments already
(``oc8.runtime.workspace``), and the next leg's output sync starts afresh.

The workspace has its own cap, ``run_workspace_state_max_bytes``. Over it,
or on any other failure, the save logs a warning, deletes the run's earlier
workspace (so the next leg starts empty, not from stale files) and returns
False; the leg itself is never failed by it.

The same ownership rules as any seed apply: ``seed_files`` is the restored
content for a ``SeedDir("/workspace", ...)`` (cli_harness) or a
``pack_files(..., prefix="workspace")`` seed (the isolated runtime).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from oc8.config import get_settings
from oc8.runtime.run_state import RunStateStore, filter_tar
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.types import SandboxHandle

logger = logging.getLogger(__name__)

WORKSPACE_PATH = "/workspace"
#: The state name the workspace is saved under.
WORKSPACE_STATE = "workspace"
#: Longest one workspace save may take, read and upload together.
SAVE_TIMEOUT_S = 300.0


def _not_saved(rel: str) -> bool:
    return rel.split("/", 1)[0] == "output"


async def load_workspace(*, tenant_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, bytes] | None:
    """The workspace the previous leg saved, or None (first leg, the last save
    was dropped, or the stored one cannot be read -- logged). Never raises:
    a leg without its workspace still runs."""
    try:
        return await RunStateStore(tenant_id).load_files(
            run_id, WORKSPACE_STATE, max_bytes=get_settings().run_workspace_state_max_bytes
        )
    except Exception:
        logger.warning(
            "run %s: the saved workspace could not be read; this leg starts with an "
            "empty workspace",
            run_id,
            exc_info=True,
        )
        return None


async def save_workspace(
    driver: SandboxDriver,
    handle: SandboxHandle,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
) -> bool:
    """Keep the sandbox's ``/workspace`` (without ``output/``) for the next
    leg. Never raises; returns whether it was saved. ``tenant_id`` must be the
    run's tenant as loaded under RLS."""
    cap = get_settings().run_workspace_state_max_bytes
    store = RunStateStore(tenant_id)

    async def _save() -> None:
        tar = await driver.get_archive(handle, WORKSPACE_PATH, max_bytes=cap)
        await store.save(run_id, WORKSPACE_STATE, filter_tar(tar, _not_saved), max_bytes=cap)

    try:
        await asyncio.wait_for(_save(), timeout=SAVE_TIMEOUT_S)
    except Exception as exc:
        logger.warning(
            "run %s: /workspace was not kept (%s: %s); the next leg starts with an empty workspace",
            run_id,
            type(exc).__name__,
            exc,
        )
        try:
            await store.delete(run_id, WORKSPACE_STATE)
        except Exception:
            logger.warning("run %s: could not drop the earlier workspace", run_id, exc_info=True)
        return False
    return True
