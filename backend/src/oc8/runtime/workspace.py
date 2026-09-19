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
"""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from asyncio import to_thread

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.storage.attachments import AttachmentRejected, store_attachment_bytes

logger = logging.getLogger(__name__)

OUTPUT_SUBDIR = "output"


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
    """
    files = await to_thread(_scan_output_dir, output_dir(run_id))
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
