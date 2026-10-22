"""Remove agent containers a dead worker left behind.

`teardown` sits in a `finally`, which covers a failing run but not a killed
process: restarting the worker mid-run skips it entirely. Observed live,
2026-07-27 -- two agent containers still running 40+ minutes after their runs had
finished, each holding a live run-scoped token and able to keep writing to the
customer's system. The `finally` is where teardown BELONGS; this is the sweep
for the one case it structurally cannot cover.

The dangerous half is not leaving a container behind, it is killing the wrong
one, so the rule is deliberately narrow: remove a container only when the run
named in its own name is FINISHED. That makes the sweep safe with several
workers on one host without any coordination between them -- a sibling's live run
is not finished, so its containers are invisible to this. Anything we cannot
resolve to a finished run is left alone: unknown is not the same as done.

One exception: a container provisioned with ``max_lifetime_s`` carries a
deadline label, and past that deadline it is removed whatever its run says.
That is how the Docker driver enforces the bound, so it holds only as tightly
as this sweep runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
import uuid
from typing import Any

from sqlalchemy import select

from oc8 import models as m
from oc8.config import Settings, get_settings
from oc8.db.session import tenant_session
from oc8.runtime.states import TERMINAL
from oc8.sandbox.docker_driver import EXPIRES_LABEL, NAMESPACE_LABEL
from oc8.sandbox.linger import EXIT_TIME_COMMAND
from oc8.triggers.scheduler import list_active_tenant_ids

logger = logging.getLogger(__name__)

#: The label the provisioner stamps a run's container with. Matching happens on
#: THIS, never on the container name: the name carries only the first 8 chars of
#: a uuid7, which is a TIMESTAMP, so two runs started in the same moment share
#: it -- and reaping by name could therefore kill a LIVE container because some
#: unrelated run of the same second had finished. Caught by a test, not in
#: production, which is the only reason it reads as a footnote.
RUN_LABEL = "oc8.run"
# The organisation a sandbox belongs to, so a deployment can find (and stop)
# everything one organisation has running.
TENANT_LABEL = "oc8.tenant"

#: Namespace stays OUT of this Docker-side filter on purpose: a container
#: built before NAMESPACE_LABEL existed carries none at all, and a filter of
#: `oc8.namespace=<ours>` would then never match it, leaking it forever. The
#: listing stays broad -- every container this HOST considers a sandbox -- and
#: `_ours_namespace` below narrows it in Python, where "no label" can be read
#: as the default namespace instead of "not ours".
_LABEL_FILTER = {"label": "oc8.sandbox=1"}


def _ours_namespace(container: Any) -> bool:
    """Whether ``container`` belongs to THIS deployment's namespace.

    Two oc8 stacks sharing one Docker host would otherwise both match
    `oc8.sandbox=1` and each would happily reap the other's containers. A
    missing label reads as the default namespace, so containers created
    before this check existed are still ours rather than suddenly foreign.
    """
    labels = getattr(container, "labels", None) or {}
    default_namespace = Settings.model_fields["deployment_namespace"].default
    namespace = labels.get(NAMESPACE_LABEL) or default_namespace
    return bool(namespace == get_settings().deployment_namespace)


#: A name this codebase would have written (see sandbox.naming). Used only for
#: the leftovers below: the label alone is not enough to claim a container.
_OUR_NAME = re.compile(r"^oc8-[a-z0-9-]+$")

#: How long an EXITED container is left alone before it counts as debris. A
#: worker elsewhere may still be reading the logs of a container that stopped
#: seconds ago -- that is how a failed run reports what went wrong -- and an hour
#: is far past any run this system produces.
_DEBRIS_AFTER = dt.timedelta(hours=1)


def _past_deadline(container: Any) -> bool:
    """Whether ``container`` has outlived its ``max_lifetime_s``.

    The deadline is a label the driver stamps at provision time, so this needs
    no inspect. A missing or unreadable one is no deadline: unknown is not the
    same as expired.
    """
    labels = getattr(container, "labels", None) or {}
    raw = labels.get(EXPIRES_LABEL)
    if not raw:
        return False
    try:
        deadline = int(raw)
    except ValueError:
        return False
    return time.time() > deadline


def run_id_of(container: Any) -> str | None:
    labels = getattr(container, "labels", None) or {}
    value = labels.get(RUN_LABEL)
    return str(value) if value else None


async def _exited_long_ago(container: Any) -> bool:
    """An exited container of ours that nothing can still be using.

    Containers started before the run label existed carry none, so the rule
    above can never resolve them and they would sit on the host forever. They
    are still ours, and an exited one holds no process, no token and no lock --
    removing it costs nothing. A RUNNING one without a label is a different
    question and is never touched: we cannot tell whose it is, and it is doing
    something.
    """
    if not _OUR_NAME.match(getattr(container, "name", "") or ""):
        return False
    # `containers.list()` gives a LIST-shaped payload where State is a plain
    # string; FinishedAt only exists after an inspect. Found live: without this
    # the rule silently matched nothing at all.
    reload_ = getattr(container, "reload", None)
    if callable(reload_):
        try:
            await asyncio.to_thread(reload_)
        except Exception:
            return False
    if str(getattr(container, "status", "")) == "running":
        # A sandbox outlives its command (oc8.sandbox.linger): a running one
        # whose command ended long ago is as finished as an exited one.
        finished = await _command_finished_at(container)
        return finished is not None and dt.datetime.now(tz=dt.UTC) - finished > _DEBRIS_AFTER
    if str(getattr(container, "status", "")) != "exited":
        return False
    state = (getattr(container, "attrs", None) or {}).get("State")
    raw = state.get("FinishedAt") if isinstance(state, dict) else None
    if not raw:
        return False
    try:
        finished = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=dt.UTC)
    return dt.datetime.now(tz=dt.UTC) - finished > _DEBRIS_AFTER


#: Longest the reaper waits for one exit-marker read.
_EXEC_TIMEOUT_S = 10.0


async def _command_finished_at(container: Any) -> dt.datetime | None:
    """When a lingering container's command ended, from its exit marker; None
    while it runs, or when that cannot be told."""
    exec_run = getattr(container, "exec_run", None)
    if not callable(exec_run):
        return None
    attrs = getattr(container, "attrs", None) or {}
    user = str((attrs.get("Config") or {}).get("User") or "")
    try:
        # As the sandbox's configured user, and bounded: the command itself is
        # killed in the sandbox after 5 s, and a daemon call that never
        # answers is abandoned here.
        code, output = await asyncio.wait_for(
            asyncio.to_thread(exec_run, EXIT_TIME_COMMAND, user=user), timeout=_EXEC_TIMEOUT_S
        )
    except Exception:
        return None
    text = (output or b"").decode("utf-8", "replace").strip()
    if int(code) != 0 or not text.isdigit():
        return None
    return dt.datetime.fromtimestamp(int(text), tz=dt.UTC)


async def _finished_runs(run_ids: set[str]) -> set[str]:
    """Which of `run_ids` name a run that has FINISHED.

    Per tenant, because RLS fails closed: an unbound session sees no runs at
    all, and the containers on one host may belong to any tenant. Same
    tenant-discovery pattern the cron scheduler uses.

    A run this sweep cannot find stays out of the result: unknown is not the
    same as finished, and a container is never destroyed on a guess.
    """
    if not run_ids:
        return set()
    wanted: set[uuid.UUID] = set()
    for raw in run_ids:
        try:
            wanted.add(uuid.UUID(raw))
        except ValueError:
            continue
    if not wanted:
        return set()

    finished: set[str] = set()
    for tenant_id in await list_active_tenant_ids():
        async with tenant_session(tenant_id) as db:
            rows = (
                await db.execute(
                    select(m.AgentRun.id).where(
                        m.AgentRun.id.in_(wanted),
                        m.AgentRun.state.in_([s.value for s in TERMINAL]),
                    )
                )
            ).scalars()
            finished.update(str(rid) for rid in rows)
    return finished


async def reap_orphaned_containers(*, client: Any | None = None) -> list[str]:
    """Remove containers whose run has finished, and containers past their
    maximum lifetime (``EXPIRES_LABEL``). Returns the names removed.

    Never raises: this runs at worker startup, and a docker hiccup must not stop
    a worker from starting. One container that refuses to die does not stop the
    others either.
    """
    try:
        if client is None:
            import docker  # imported here so tests need no daemon

            client = docker.from_env()  # type: ignore[attr-defined]
        containers = await asyncio.to_thread(
            lambda: client.containers.list(all=True, filters=dict(_LABEL_FILTER))
        )
    except Exception:
        logger.warning("could not list containers to reap; skipping", exc_info=True)
        return []

    by_run: dict[str, list[Any]] = {}
    debris: list[Any] = []
    expired: list[Any] = []
    for c in containers:
        if not _ours_namespace(c):
            continue
        if _past_deadline(c):
            # The caller asked for this bound when it provisioned; the run's
            # own state does not get a say. This is the one rule here that
            # removes a LIVE container, and only one that carries the label.
            expired.append(c)
            continue
        run_id = run_id_of(c)
        if run_id is not None:
            by_run.setdefault(run_id, []).append(c)
        elif await _exited_long_ago(c):
            debris.append(c)

    try:
        finished = await _finished_runs(set(by_run))
    except Exception:
        logger.warning("could not resolve runs while reaping; skipping", exc_info=True)
        return []

    removed: list[str] = []
    for c in expired:
        try:
            await asyncio.to_thread(c.remove, force=True, v=True)
        except Exception:
            logger.warning("could not remove expired container %s", c.name, exc_info=True)
            continue
        removed.append(c.name)
        logger.info("removed container %s (past its maximum lifetime)", c.name)

    for c in debris:
        try:
            await asyncio.to_thread(c.remove, force=True, v=True)
        except Exception:
            logger.warning("could not remove stopped container %s", c.name, exc_info=True)
            continue
        removed.append(c.name)
        logger.info("removed stopped container %s (no run label, long finished)", c.name)

    for run_id in sorted(finished):
        for c in by_run[run_id]:
            try:
                await asyncio.to_thread(c.remove, force=True, v=True)
            except Exception:
                logger.warning("could not remove leftover container %s", c.name, exc_info=True)
                continue
            removed.append(c.name)
            logger.info("removed leftover container %s (run %s finished)", c.name, run_id)
    if removed:
        logger.info("reaped %d leftover agent container(s)", len(removed))
    return removed
