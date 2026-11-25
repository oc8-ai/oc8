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
