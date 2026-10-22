"""Agent containers left behind by a worker that died mid-run.

Teardown lives in a `finally`, which covers a failing run but not a killed
process: a `docker compose up -d --build worker` during a run skips it entirely.
Observed live, 2026-07-27: two agent containers still running 40+ minutes after
their runs had finished -- each holding a live run-scoped token, each able to
keep writing to the customer's system.

The dangerous half of this is not leaving one behind, it is killing the wrong
one, so the rule is narrow: remove a container only when the run named IN ITS
OWN NAME is finished. A sibling worker's live run is not finished, so its
containers are never touched.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.sandbox.naming import container_name
from oc8.sandbox.reaper import reap_orphaned_containers
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeContainer:
    def __init__(
        self, name: str, run_id: str | None = None, *, namespace: str | None = None
    ) -> None:
        self.name = name
        self.status = "running"
        self.attrs: dict[str, Any] = {}
        # Identity lives in the label, not the name — see reaper.RUN_LABEL.
        self.labels = (
            {"oc8.sandbox": "1"}
            | ({"oc8.run": run_id} if run_id else {})
            | ({"oc8.namespace": namespace} if namespace else {})
        )
        self.removed = False

    def remove(self, force: bool = False, v: bool = False) -> None:
        self.removed = True
        self.removed_volumes = v


class _FakeDocker:
    """Stands in for the docker client: only what the reaper touches."""

    def __init__(self, named: list[tuple[str, str | None]]) -> None:
        self.items = [_FakeContainer(n, r) for n, r in named]
        self.listed_filters: dict[str, Any] | None = None

    class _Containers:
        def __init__(self, outer: _FakeDocker) -> None:
            self.outer = outer

        def list(self, all: bool = False, filters: dict[str, Any] | None = None) -> list[Any]:
            self.outer.listed_filters = filters
            return self.outer.items

    @property
    def containers(self) -> Any:
        return _FakeDocker._Containers(self)

    def removed_names(self) -> list[str]:
        return [c.name for c in self.items if c.removed]


async def _run(db: Any, tenant: uuid.UUID, state: str) -> uuid.UUID:
    # A real Organization row: the sweep discovers tenants the same way the cron
    # scheduler does, because RLS fails closed and an unbound session sees no
    # runs at all.
    if await db.get(m.Organization, tenant) is None:
        db.add(
            m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T")
        )
        await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Sina")
    db.add(agent)
    await db.flush()
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state=state, context={})
    db.add(run)
    await db.flush()
    return run.id


async def test_a_container_of_a_finished_run_is_removed(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run_id = await _run(db, tenant, "done")
        await db.commit()

    name = container_name("Sina", "agent", run_id)
    docker = _FakeDocker([(name, str(run_id))])
    removed = await reap_orphaned_containers(client=docker)

    assert removed == [name]
    assert docker.removed_names() == [name]
    # Ours only: a stray container on the same host is none of our business.
    assert (docker.listed_filters or {}).get("label") == "oc8.sandbox=1"


async def test_a_container_of_a_live_run_is_left_alone(
    app_session: AppSessionFactory,
) -> None:
    """The whole risk of this feature. A sibling worker's run is RUNNING, and
    killing its container would abort real work mid-flight."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        running = await _run(db, tenant, "running")
        queued = await _run(db, tenant, "queued")
        parked = await _run(db, tenant, "waiting_for_approval")
        await db.commit()

    docker = _FakeDocker(
        [(container_name("Sina", "agent", r), str(r)) for r in (running, queued, parked)]
    )
    assert await reap_orphaned_containers(client=docker) == []
    assert docker.removed_names() == []


async def test_a_container_in_a_foreign_namespace_is_left_alone(
    app_session: AppSessionFactory,
) -> None:
    """Two oc8 stacks can share one Docker host. A finished run belonging to
    the OTHER deployment's namespace must never be touched by this one."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run_id = await _run(db, tenant, "done")
        await db.commit()

    name = container_name("Sina", "agent", run_id)
    docker = _FakeDocker([])
    docker.items = [_FakeContainer(name, str(run_id), namespace="staging")]
    assert await reap_orphaned_containers(client=docker) == []
    assert docker.removed_names() == []


async def test_a_container_with_no_namespace_label_is_treated_as_default(
    app_session: AppSessionFactory,
) -> None:
    """Containers built before the namespace label existed carry none at all.
    A default-namespace deployment must still reap these -- a check added
    later cannot make its own earlier containers look foreign."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run_id = await _run(db, tenant, "done")
        await db.commit()

    name = container_name("Sina", "agent", run_id)
    docker = _FakeDocker([(name, str(run_id))])  # no namespace label at all
    assert await reap_orphaned_containers(client=docker) == [name]


async def test_a_container_whose_run_is_unknown_is_left_alone(
    app_session: AppSessionFactory,
) -> None:
    """Unknown is not the same as finished. A run this process cannot see -- a
    different tenant, a name we cannot parse, a container from something else
    entirely -- must never be destroyed on a guess."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _run(db, tenant, "done")
        await db.commit()

    ghost = uuid.uuid4()
    docker = _FakeDocker(
        [
            (container_name("Sina", "agent", ghost), str(ghost)),  # no such run
            ("oc8-someplugin-hook-abc123", None),  # pooled worker, tied to no run
            ("tender_lamarr", None),  # not ours at all
        ]
    )
    assert await reap_orphaned_containers(client=docker) == []
    assert docker.removed_names() == []


async def test_one_container_that_will_not_die_does_not_stop_the_others(
    app_session: AppSessionFactory,
) -> None:
    """This runs at worker startup. A docker hiccup on one container must not
    prevent the worker from starting, nor leave the rest behind."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first = await _run(db, tenant, "done")
        second = await _run(db, tenant, "failed")
        await db.commit()

    names = [container_name("Sina", "agent", first), container_name("Sina", "agent", second)]
    docker = _FakeDocker([(names[0], str(first)), (names[1], str(second))])

    def explode(force: bool = False) -> None:
        raise RuntimeError("docker said no")

    docker.items[0].remove = explode  # type: ignore[method-assign]

    removed = await reap_orphaned_containers(client=docker)
    assert removed == [names[1]]


# --------------------------------------------------------- containers from before

# Containers started before the run label existed carry no `oc8.run`, so the rule
# above can never resolve them and they would sit on the host forever. They are
# still ours -- our label, our naming -- and an EXITED one holds no process, no
# token and no lock, so removing it costs nothing. A RUNNING one is a different
# question entirely and is never touched.


def _exited(name: str, *, minutes_ago: int) -> _FakeContainer:
    c = _FakeContainer(name)
    # The LIST payload has no FinishedAt at all -- it appears only after the
    # inspect the reaper performs, which is what `reload` stands in for here.
    # Found live: a fake that skipped that step passed while the real thing
    # matched nothing.
    c.status = "created"
    c.attrs = {"State": "exited"}

    def _reload() -> None:
        c.status = "exited"
        c.attrs = {
            "State": {
                "FinishedAt": (
                    dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes_ago)
                ).isoformat()
            }
        }

    c.reload = _reload  # type: ignore[attr-defined]
    return c


async def test_an_old_exited_container_without_a_run_label_is_removed(
    app_session: AppSessionFactory,
) -> None:
    docker = _FakeDocker([])
    docker.items = [_exited("oc8-nora-agent-019fa522-d093ec", minutes_ago=180)]
    assert await reap_orphaned_containers(client=docker) == [
        "oc8-nora-agent-019fa522-d093ec"
    ]


async def test_one_that_only_just_exited_is_given_time(
    app_session: AppSessionFactory,
) -> None:
    """A worker elsewhere may still be reading the logs of a container that
    exited seconds ago -- that is how a failed run reports what went wrong."""
    docker = _FakeDocker([])
    docker.items = [_exited("oc8-nora-agent-019fa522-d093ec", minutes_ago=2)]
    assert await reap_orphaned_containers(client=docker) == []


async def test_a_running_container_without_a_run_label_is_never_touched(
    app_session: AppSessionFactory,
) -> None:
    """The one that matters. Without a run label we cannot tell whose it is, and
    it is doing something -- killing it would abort live work."""
    docker = _FakeDocker([])
    running = _FakeContainer("oc8-nora-agent-019fa522-d093ec")
    running.status = "running"
    running.attrs = {"State": {"Running": True, "FinishedAt": "0001-01-01T00:00:00Z"}}
    docker.items = [running]
    assert await reap_orphaned_containers(client=docker) == []


def _lingering(name: str, *, minutes_ago: int | None) -> _FakeContainer:
    """A running container whose command finished ``minutes_ago`` (None: still
    running): the wrapper keeps it up and records the end in /.oc8/exit."""
    c = _FakeContainer(name)
    c.status = "running"
    c.attrs = {"State": {"Running": True, "FinishedAt": "0001-01-01T00:00:00Z"}}

    def exec_run(cmd: list[str], **kw: Any) -> tuple[int, bytes]:
        if minutes_ago is None:
            return 1, b""
        finished = dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes_ago)
        return 0, f"{int(finished.timestamp())}\n".encode()

    c.exec_run = exec_run  # type: ignore[attr-defined]
    return c


async def test_a_lingering_container_whose_command_ended_long_ago_is_removed(
    app_session: AppSessionFactory,
) -> None:
    """A sandbox outlives its command (oc8.sandbox.linger), so a container
    without a run label (a pooled hook worker) never reaches `exited`. Its
    command having ended long ago makes it debris all the same."""
    docker = _FakeDocker([])
    docker.items = [
        _lingering("oc8-nora-hook-019fa522-d093ec", minutes_ago=180),
        _lingering("oc8-nora-hook-019fa522-aaaaaa", minutes_ago=2),
        _lingering("oc8-nora-hook-019fa522-bbbbbb", minutes_ago=None),
    ]
    assert await reap_orphaned_containers(client=docker) == ["oc8-nora-hook-019fa522-d093ec"]


async def test_a_stranger_is_left_alone_even_when_exited(
    app_session: AppSessionFactory,
) -> None:
    """The label alone is not enough: the name has to be one WE would have
    written. A container labelled by something else is none of our business."""
    docker = _FakeDocker([])
    docker.items = [_exited("tender_lamarr", minutes_ago=999)]
    assert await reap_orphaned_containers(client=docker) == []


# ------------------------------------------------------------- lifetime bound

# A spec's `max_lifetime_s` reaches Docker as a deadline label. Past it, the
# container is removed whatever its run says: the caller asked for the bound.


async def test_a_container_past_its_deadline_is_removed_even_while_its_run_is_live(
    app_session: AppSessionFactory,
) -> None:
    import time

    from oc8.sandbox.reaper import EXPIRES_LABEL

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        running = await _run(db, tenant, "running")
        await db.commit()

    name = container_name("Sina", "agent", running)
    docker = _FakeDocker([(name, str(running))])
    docker.items[0].labels[EXPIRES_LABEL] = str(int(time.time()) - 5)

    assert await reap_orphaned_containers(client=docker) == [name]
    # Scratch lives in anonymous volumes; they go with the container.
    assert docker.items[0].removed_volumes is True


async def test_a_container_before_its_deadline_is_left_alone(
    app_session: AppSessionFactory,
) -> None:
    import time

    from oc8.sandbox.reaper import EXPIRES_LABEL

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        running = await _run(db, tenant, "running")
        await db.commit()

    docker = _FakeDocker([(container_name("Sina", "agent", running), str(running))])
    docker.items[0].labels[EXPIRES_LABEL] = str(int(time.time()) + 600)
    assert await reap_orphaned_containers(client=docker) == []


async def test_an_unreadable_deadline_is_not_a_deadline(
    app_session: AppSessionFactory,
) -> None:
    from oc8.sandbox.reaper import EXPIRES_LABEL

    docker = _FakeDocker([])
    running = _FakeContainer("oc8-nora-agent-019fa522-d093ec")
    running.labels[EXPIRES_LABEL] = "soon"
    docker.items = [running]
    assert await reap_orphaned_containers(client=docker) == []
