"""The run worker's housekeeping, as public functions.

Every process that consumes the run queue must run these sweeps: Community's
`oc8 worker`, and any edition that wraps the run worker in its own entrypoint.
They used to live inside the CLI, so an edition with its own entrypoint had to
copy the list -- and one that did not copy it ran none of them: a run whose
worker died stayed `running` for good and kept its agent busy, queued runs
whose publish was lost were never started, and knowledge reconciliation never
ran. Pass `run_worker_housekeeping` as the run worker's `housekeeping` and call
`run_worker_startup` once before consuming.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from oc8.connections.consolidate import consolidate_all_tenants
from oc8.evidence.sweep import sweep_evidence
from oc8.knowledge.reconcile import sweep_knowledge_deletions
from oc8.notifications.service import prune_read_notifications
from oc8.records.registry import prune_expired_links
from oc8.runtime.reconcile import (
    close_abandoned_runs,
    recover_stranded_runs,
    release_stale_running_agents,
)
from oc8.sandbox import get_sandbox_driver

logger = logging.getLogger(__name__)


async def _reap_orphans() -> Any:
    return await get_sandbox_driver().reap_orphans()


async def _run(name: str, sweep: Callable[[], Awaitable[Any]]) -> None:
    # One failing sweep must not skip the ones after it: a broken evidence
    # archive would otherwise also stop runs being freed.
    try:
        await sweep()
    except Exception:
        logger.exception("worker housekeeping: %s failed", name)


async def run_worker_startup() -> None:
    """Once, before consuming. A worker killed mid-run never ran its teardown,
    so its containers are still alive with a live run-scoped token: only
    containers of FINISHED runs are removed, so this is safe with several
    workers. Then a run committed as queued whose stream entry never landed is
    published again -- the timer repeats it, this pass makes a restart the
    recovery. First of all, old per-department tool setups are folded into
    shared logins (`connections.consolidate`; once per tenant, a no-op after
    that), so no run starts on a setup about to change."""
    await _run("consolidate_connections", consolidate_all_tenants)
    await _run("reap_orphans", _reap_orphans)
    await _run("recover_stranded_runs", recover_stranded_runs)


async def run_worker_housekeeping() -> None:
    """One tick of the run worker's timer. Order matters:

    1. close runs whose executor stopped beating, freeing their agents;
    2. free agents shown "running" whose run's process died before its commit;
    3. republish queued runs nothing will start -- after 1, so a run whose
       agent this tick freed is already a candidate;
    4. reap containers of finished runs;
    5. archive evidence (inert unless OC8_EVIDENCE_SWEEP_ENABLED) -- after the
       close, so a run closed this tick is archived on the next one;
    6. remove knowledge a sync observed as gone upstream (inert unless
       OC8_KNOWLEDGE_RECONCILE_ENABLED; no network IO, no connector calls);
    7. delete issued record links past their TTL;
    8. delete member notifications read more than 90 days ago.

    Only the run queue gets these -- an ingestion job has no containers, no
    agent to free and no evidence on disk. A sandbox runtime syncs its own
    /workspace/output while the leg runs, so there is no output sweep here.
    """
    await _run("close_abandoned_runs", close_abandoned_runs)
    await _run("release_stale_running_agents", release_stale_running_agents)
    await _run("recover_stranded_runs", recover_stranded_runs)
    await _run("reap_orphans", _reap_orphans)
    await _run("sweep_evidence", sweep_evidence)
    await _run("sweep_knowledge_deletions", sweep_knowledge_deletions)
    await _run("prune_expired_links", prune_expired_links)
    await _run("prune_read_notifications", prune_read_notifications)


__all__ = ["run_worker_housekeeping", "run_worker_startup"]
