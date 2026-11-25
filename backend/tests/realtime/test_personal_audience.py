"""A4 on the live socket: what a personal agent is doing reaches its owner only.

The feed socket (`realtime/ws.py`) hands an every-agent run reader every
envelope of the tenant that names no audience. A personal agent's content
events -- streamed tokens, reasoning, tool calls, step timings, container
output, rendered components, todos and `activity.logged` -- therefore go out
addressed to the department's owner (`publish_member_event`), and to nobody
when the owner has left. Metadata stays tenant-wide (R2): `run.status`.

The routing is checked end to end on the envelope: what the emitters hand the
bus is routed through `realtime.manager._route` for an admin's feed socket and
the owner's feed socket, exactly as `ConnectionManager` would.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from oc8 import models as m
from oc8.authz.permissions import SEAT_APPROVER
from oc8.copilot.profile import offboard_member
from oc8.realtime import audience as audience_mod
from oc8.realtime import emit
from oc8.realtime.envelope import build_envelope
from oc8.realtime.manager import _Conn, _route
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SECRET = "the owner's private answer"

#: Every content event a personal run's live view carries.
_CONTENT_TYPES = {
    "run.token_delta",
    "run.reasoning_delta",
    "run.tool_call",
    "run.output_delta",
    "run.step_timing",
    "run.component_rendered",
    "run.todos_updated",
    "activity.logged",
}


class _NullWS:
    async def send_text(self, data: str) -> None:  # pragma: no cover - never sent
        raise AssertionError("routed by hand")


class _SpyBus:
    """Builds the envelopes `EventBus` would put on the tenant channel."""

    def __init__(self) -> None:
        self.raw: list[str] = []
        self.tenant_wide: list[str] = []
        self.gone: list[uuid.UUID] = []

    async def publish_event(
        self, tenant_id: uuid.UUID, type_: str, data: dict[str, Any], *, source: str
    ) -> None:
        self.tenant_wide.append(type_)
        self.raw.append(json.dumps(build_envelope(tenant_id, type_, data, source)))

    async def publish_member_event(
        self,
        tenant_id: uuid.UUID,
        member_ids: Sequence[uuid.UUID],
        type_: str,
        data: dict[str, Any],
        *,
        source: str,
    ) -> None:
        if not member_ids:
            return
        self.raw.append(
            json.dumps(build_envelope(tenant_id, type_, data, source, audience=member_ids))
        )

    async def publish_member_gone(self, tenant_id: uuid.UUID, member_id: uuid.UUID) -> None:
        self.gone.append(member_id)


@dataclass(frozen=True)
class _World:
    tenant: uuid.UUID
    owner: uuid.UUID
    admin: uuid.UUID
    pia: uuid.UUID  # personal
    nora: uuid.UUID  # standard


async def _world(app_session: AppSessionFactory) -> _World:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={}))
        shared = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        owner = m.OrgMember(
            tenant_id=tenant, subject="owner", subject_uuid=uuid.uuid4(), display_name="Owner"
        )
        admin = m.OrgMember(
            tenant_id=tenant, subject="admin", subject_uuid=uuid.uuid4(), display_name="Admin"
        )
        db.add_all([shared, owner, admin])
        await db.flush()
        personal = m.Department(
            tenant_id=tenant,
            name="Owner",
            frame={"closed": True},
            kind="personal",
            owner_member_id=owner.id,
        )
        db.add(personal)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=owner.id,
                department_id=personal.id,
                seat_role=SEAT_APPROVER,
                agent_manage=True,
            )
        )
        pia = m.Agent(tenant_id=tenant, department_id=personal.id, name="Pia")
        nora = m.Agent(tenant_id=tenant, department_id=shared.id, name="Nora")
        db.add_all([pia, nora])
        await db.flush()
        return _World(tenant, owner.id, admin.id, pia.id, nora.id)


async def _emit_everything(
    tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID, *, db: Any = None
) -> None:
    """Every content emitter and direct publisher, once, plus the status."""
    await emit.publish_run_token_delta(tenant, run_id=run_id, agent_id=agent_id, text=_SECRET)
    await emit.publish_run_reasoning_delta(
        tenant, run_id=run_id, agent_id=agent_id, step=1, text=_SECRET
    )
    await emit.publish_run_tool_call(
        tenant, run_id=run_id, agent_id=agent_id, call={"tool": "search", "arguments": _SECRET}
    )
    await emit.publish_run_output_delta(tenant, run_id=run_id, agent_id=agent_id, chunk=_SECRET)
    await emit.publish_run_step_timing(
        tenant, run_id=run_id, agent_id=agent_id, timing={"step": 1, "reasoning": _SECRET}
    )
    await emit.publish_run_content(
        db,
        tenant_id=tenant,
        agent_id=agent_id,
        event={
            "type": "run.component_rendered",
            "data": {"run_id": str(run_id), "props": {"title": _SECRET}},
            "source": f"oc8/run/{run_id}",
        },
    )
    await emit.publish_run_content(
        db,
        tenant_id=tenant,
        agent_id=agent_id,
        event={
            "type": "run.todos_updated",
            "data": {"run_id": str(run_id), "todos": [{"content": _SECRET}]},
            "source": f"oc8/run/{run_id}",
        },
    )
    # The status is metadata (R2): the repository publishes it tenant-wide.
    await emit.get_event_bus().publish_event(
        tenant,
        "run.status",
        {"run_id": str(run_id), "agent_id": str(agent_id), "state": "running"},
        source=f"oc8/run/{run_id}",
    )


async def _record_activity(app_session: AppSessionFactory, w: _World, agent_id: uuid.UUID) -> None:
    async with app_session(w.tenant) as db:
        await emit.record_activity(
            db, tenant_id=w.tenant, agent_id=agent_id, status="info", message=_SECRET
        )


def _received(bus: _SpyBus, member_id: uuid.UUID) -> list[dict[str, Any]]:
    conn = _Conn(ws=_NullWS(), member_id=str(member_id), feed=True)
    frames = [_route(conn, raw)[0] for raw in bus.raw]
    return [json.loads(f) for f in frames if f is not None]


@pytest.fixture
def spy_bus(monkeypatch: pytest.MonkeyPatch) -> _SpyBus:
    bus = _SpyBus()
    monkeypatch.setattr(emit, "get_event_bus", lambda: bus)
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: bus)
    return bus


async def test_admin_feed_gets_status_but_no_content_of_a_personal_run(
    app_session: AppSessionFactory, spy_bus: _SpyBus
) -> None:
    """Review focus 4: an admin's feed socket, connected before the owner's
    run starts, sees that it runs -- and nothing of what it says."""
    w = await _world(app_session)
    await _emit_everything(w.tenant, w.pia, uuid.uuid4())
    await _record_activity(app_session, w, w.pia)

    got = _received(spy_bus, w.admin)
    assert [e["type"] for e in got] == ["run.status"]
    assert all(_SECRET not in json.dumps(e) for e in got)
    assert not (_CONTENT_TYPES & set(spy_bus.tenant_wide))


async def test_owner_feed_gets_every_content_event(
    app_session: AppSessionFactory, spy_bus: _SpyBus
) -> None:
    w = await _world(app_session)
    await _emit_everything(w.tenant, w.pia, uuid.uuid4())
    await _record_activity(app_session, w, w.pia)

    got = _received(spy_bus, w.owner)
    assert {e["type"] for e in got} == _CONTENT_TYPES | {"run.status"}
    assert all("audience" not in e for e in got)


async def test_a_departed_owners_content_goes_to_nobody(
    app_session: AppSessionFactory, spy_bus: _SpyBus
) -> None:
    """Review focus 2: the owner has left -- the content goes to nobody, the
    status still to everybody."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        owner = await db.get(m.OrgMember, w.owner)
        assert owner is not None
        owner.deleted_at = dt.datetime.now(dt.UTC)
        await offboard_member(db, tenant_id=w.tenant, member_id=w.owner)
    await _emit_everything(w.tenant, w.pia, uuid.uuid4())
    await _record_activity(app_session, w, w.pia)

    for member in (w.owner, w.admin):
        assert [e["type"] for e in _received(spy_bus, member)] == ["run.status"]


async def test_an_owner_who_leaves_after_the_audience_was_cached_gets_nothing_more(
    app_session: AppSessionFactory, spy_bus: _SpyBus
) -> None:
    """The cache (R5) is dropped for an owner who leaves, once the offboarding
    commits: the next event finds the department without an owner."""
    w = await _world(app_session)
    run_id = uuid.uuid4()
    await emit.publish_run_token_delta(w.tenant, run_id=run_id, agent_id=w.pia, text="before")
    assert [e["data"]["text"] for e in _received(spy_bus, w.owner)] == ["before"]

    async with app_session(w.tenant) as db:
        owner = await db.get(m.OrgMember, w.owner)
        assert owner is not None
        owner.deleted_at = dt.datetime.now(dt.UTC)
        await offboard_member(db, tenant_id=w.tenant, member_id=w.owner)
    await asyncio.sleep(0)  # the after-commit hook runs as a task

    spy_bus.raw.clear()
    await emit.publish_run_token_delta(w.tenant, run_id=run_id, agent_id=w.pia, text="after")
    assert spy_bus.raw == []


async def test_standard_agent_events_stay_tenant_wide(
    app_session: AppSessionFactory, spy_bus: _SpyBus
) -> None:
    w = await _world(app_session)
    await _emit_everything(w.tenant, w.nora, uuid.uuid4())
    await _record_activity(app_session, w, w.nora)

    expected = _CONTENT_TYPES | {"run.status"}
    assert set(spy_bus.tenant_wide) == expected
    for member in (w.owner, w.admin):
        assert {e["type"] for e in _received(spy_bus, member)} == expected


async def test_the_callers_uncommitted_session_resolves_the_agent(
    app_session: AppSessionFactory, spy_bus: _SpyBus
) -> None:
    """The in-process engine publishes inside its own tenant-bound transaction,
    where the agent may not be committed yet: the lookup uses that session."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        dept = (await db.get(m.Agent, w.pia)).department_id  # type: ignore[union-attr]
        fresh = m.Agent(tenant_id=w.tenant, department_id=dept, name="Neu")
        db.add(fresh)
        await db.flush()
        await emit.publish_run_content(
            db,
            tenant_id=w.tenant,
            agent_id=fresh.id,
            event={"type": "run.todos_updated", "data": {"todos": []}, "source": "t"},
        )
        await db.rollback()
    assert [e["type"] for e in _received(spy_bus, w.owner)] == ["run.todos_updated"]
    assert _received(spy_bus, w.admin) == []


async def test_an_unknown_agent_publishes_nothing(
    app_session: AppSessionFactory, spy_bus: _SpyBus
) -> None:
    """Fails closed: an agent the lookup cannot see is not assumed standard."""
    w = await _world(app_session)
    await emit.publish_run_token_delta(
        w.tenant, run_id=uuid.uuid4(), agent_id=uuid.uuid4(), text=_SECRET
    )
    assert spy_bus.raw == []


async def test_audience_cache_is_keyed_by_agent(
    app_session: AppSessionFactory, spy_bus: _SpyBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: one database lookup per agent, not per event -- None (a standard
    agent) cached too."""
    w = await _world(app_session)
    lookups: list[uuid.UUID] = []
    real = audience_mod._lookup

    async def _counting(db: Any, agent_id: uuid.UUID) -> Any:
        lookups.append(agent_id)
        return await real(db, agent_id)

    monkeypatch.setattr(audience_mod, "_lookup", _counting)
    run_id = uuid.uuid4()
    for agent_id in (w.pia, w.pia, w.nora, w.nora):
        await emit.publish_run_token_delta(w.tenant, run_id=run_id, agent_id=agent_id, text="x")
    assert lookups == [w.pia, w.nora]
    assert audience_mod._CACHE_MAXSIZE == 4096


async def test_approval_push_goes_only_to_the_audience(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member-addressed `approval.created` (a personal agent's card, Task 3)
    pushes to that member's subscriptions, never to the whole tenant's."""
    from oc8.realtime import bus as bus_module

    tenant, owner = uuid.uuid4(), uuid.uuid4()
    tenant_calls: list[Any] = []
    member_calls: list[dict[str, Any]] = []

    async def _tenant(*args: object, **kwargs: object) -> None:
        tenant_calls.append(kwargs)

    async def _members(
        db: object, *, tenant_id: uuid.UUID, member_ids: Sequence[uuid.UUID], payload: dict
    ) -> None:
        member_calls.append({"tenant_id": tenant_id, "member_ids": list(member_ids), **payload})

    monkeypatch.setattr(bus_module, "send_push_for_tenant", _tenant)
    monkeypatch.setattr(bus_module, "send_push_to_members", _members)
    bus = bus_module.EventBus(redis_url)
    try:
        await bus.publish_member_event(
            tenant,
            [owner],
            "approval.created",
            {"approval_id": str(uuid.uuid4()), "title": "Mail senden?", "detail": _SECRET},
            source="test",
        )
        await asyncio.gather(*bus._background_tasks)
    finally:
        await bus.close()
    assert tenant_calls == []
    assert member_calls == [
        {
            "tenant_id": tenant,
            "member_ids": [owner],
            "title": "Mail senden?",
            "body": _SECRET,
            "url": "/workspace",
        }
    ]


async def _offboard(app_session: AppSessionFactory, w: _World) -> None:
    async with app_session(w.tenant) as db:
        owner = await db.get(m.OrgMember, w.owner)
        assert owner is not None
        owner.deleted_at = dt.datetime.now(dt.UTC)
        await offboard_member(db, tenant_id=w.tenant, member_id=w.owner)
    await asyncio.sleep(0)  # the after-commit hook runs as a task
    await asyncio.sleep(0)


async def test_owner_answers_expire_and_standard_ones_do_not(
    app_session: AppSessionFactory, spy_bus: _SpyBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T2-I1: `forget_owner` reaches only the offboarding's process, so
    every other one re-reads an owner within `OWNER_TTL_SECONDS`. A standard
    agent never changes and is not re-read; an unknown agent briefly cached."""
    w = await _world(app_session)
    now = [1000.0]
    monkeypatch.setattr(audience_mod, "_clock", lambda: now[0])
    lookups: list[uuid.UUID] = []
    real = audience_mod._lookup

    async def _counting(db: Any, agent_id: uuid.UUID) -> Any:
        lookups.append(agent_id)
        return await real(db, agent_id)

    monkeypatch.setattr(audience_mod, "_lookup", _counting)
    ghost = uuid.uuid4()
    run_id = uuid.uuid4()

    async def _tick() -> None:
        for agent_id in (w.pia, w.nora, ghost):
            await emit.publish_run_token_delta(w.tenant, run_id=run_id, agent_id=agent_id, text="x")

    await _tick()
    assert lookups == [w.pia, w.nora, ghost]
    now[0] += audience_mod.UNKNOWN_TTL_SECONDS + 0.1
    await _tick()
    assert lookups[3:] == [ghost]
    now[0] += audience_mod.OWNER_TTL_SECONDS
    lookups.clear()
    await _tick()
    assert lookups == [w.pia, ghost]
    assert audience_mod.OWNER_TTL_SECONDS == 60.0


async def test_another_process_stops_reaching_a_departed_owner_within_the_ttl(
    app_session: AppSessionFactory, spy_bus: _SpyBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that never ran `forget_owner` (a worker) still holds the
    owner; once the TTL passes it finds the department ownerless."""
    w = await _world(app_session)
    now = [1000.0]
    monkeypatch.setattr(audience_mod, "_clock", lambda: now[0])
    run_id = uuid.uuid4()
    await emit.publish_run_token_delta(w.tenant, run_id=run_id, agent_id=w.pia, text="a")
    # The offboarding commits in ANOTHER process: no forget_owner here.
    monkeypatch.setattr(audience_mod, "forget_owner", lambda member_id: None)
    await _offboard(app_session, w)
    now[0] += audience_mod.OWNER_TTL_SECONDS + 0.1
    spy_bus.raw.clear()
    await emit.publish_run_token_delta(w.tenant, run_id=run_id, agent_id=w.pia, text="b")
    assert spy_bus.raw == []


async def test_a_lookup_straddling_forget_owner_caches_nothing_stale(
    app_session: AppSessionFactory, spy_bus: _SpyBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T2-I1: a lookup that read the owner, then saw the offboarding
    commit and `forget_owner` run before it could cache, must not cache the
    owner it read."""
    w = await _world(app_session)
    real = audience_mod._lookup
    calls: list[Any] = []

    async def _straddling(db: Any, agent_id: uuid.UUID) -> Any:
        answer = await real(db, agent_id)
        calls.append(answer)
        if len(calls) == 1:
            assert answer.owner_member_id == w.owner
            await _offboard(app_session, w)  # commits, runs forget_owner
        return answer

    monkeypatch.setattr(audience_mod, "_lookup", _straddling)
    await emit.publish_run_token_delta(w.tenant, run_id=uuid.uuid4(), agent_id=w.pia, text="x")
    held = audience_mod.cached(w.pia)
    assert held is not None
    assert held.personal and held.owner_member_id is None
    assert spy_bus.raw == []


async def test_offboarding_announces_the_departure_after_its_commit(
    app_session: AppSessionFactory, spy_bus: _SpyBus
) -> None:
    w = await _world(app_session)
    await _offboard(app_session, w)
    assert spy_bus.gone == [w.owner]


async def test_member_gone_closes_that_members_sockets_and_reaches_no_browser(
    redis_url: str,
) -> None:
    """Ruling T2-I2: the control envelope closes the departed member's
    sockets on every replica, and is never sent to anybody as a frame."""
    from oc8.realtime.bus import EventBus
    from oc8.realtime.manager import ConnectionManager

    class _WS:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.closed: int | None = None

        async def send_text(self, data: str) -> None:
            self.sent.append(data)

        async def close(self, code: int = 1000) -> None:
            self.closed = code

    tenant, gone, stays = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mgr = ConnectionManager(redis_url)
    bus = EventBus(redis_url)
    feed_ws, member_ws, other_ws, anon_ws = _WS(), _WS(), _WS(), _WS()
    await mgr.connect(tenant, feed_ws, member_id=gone, feed=True)
    await mgr.connect(tenant, member_ws, member_id=gone, feed=False)
    await mgr.connect(tenant, other_ws, member_id=stays, feed=True)
    await mgr.connect(tenant, anon_ws)
    try:
        await bus.publish_member_gone(tenant, gone)
        for _ in range(100):
            if feed_ws.closed and member_ws.closed:
                break
            await asyncio.sleep(0.02)
        assert feed_ws.closed == 1008 and member_ws.closed == 1008
        assert other_ws.closed is None and anon_ws.closed is None
        # The tenant's channel still works for the others, and no control
        # frame reached any socket.
        await bus.publish_event(tenant, "agent.status", {"agent_id": "a"}, source="t")
        for _ in range(100):
            if other_ws.sent:
                break
            await asyncio.sleep(0.02)
        for ws in (feed_ws, member_ws, other_ws, anon_ws):
            assert all("oc8_control" not in frame for frame in ws.sent)
        assert [json.loads(f)["type"] for f in other_ws.sent] == ["agent.status"]
        assert feed_ws.sent == [] and member_ws.sent == []
    finally:
        await bus.close()
        await mgr.close()


def test_a_control_envelope_is_never_routed_to_a_socket() -> None:
    from oc8.realtime.bus import CONTROL_MEMBER_GONE, control_envelope

    raw = control_envelope(CONTROL_MEMBER_GONE, member_id=str(uuid.uuid4()))
    for conn in (
        _Conn(ws=_NullWS(), member_id=None, feed=True),
        _Conn(ws=_NullWS(), member_id=str(uuid.uuid4()), feed=False),
    ):
        assert _route(conn, raw) == (None, None)
