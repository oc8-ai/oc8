"""A polled trigger on a login a department owner owns: only the owner, as
the trigger's creator acting for them, may listen on it; it is polled only
while the owner's confirmation holds (pending: skipped, cursor kept, caught
up afterwards at most `MAX_EVENTS_PER_TICK` a tick); it stops when the login
or the owner goes, and the organisation's setup never fills its settings.
A trigger on an organisation login behaves as before."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.connections.confirmation import stamp_trigger, trigger_confirmed
from oc8.credentials.service import delete_credential
from oc8.events import sources
from oc8.events.sources import (
    EVENT_TARGET_CHANGED,
    MAX_EVENTS_PER_TICK,
    NEEDS_CONFIRMATION,
    LoginTarget,
    connection_setup_value,
    poll_tick,
    polled_event_spec,
)
from oc8.runtime.cancellation import AGENT_ARCHIVED
from oc8.runtime.person import whose_run
from oc8.runtime.queue import RunQueue
from oc8.triggers.service import (
    InvalidTriggerConfig,
    PolledTargetMissing,
    create_trigger,
    polled_event_target,
    update_trigger,
)
from tests.conftest import AppSessionFactory
from tests.connections.helpers import member
from tests.departments.owned import owned_world
from tests.events.polled import (
    SECRET,
    TOKEN,
    FakePoller,
    OwnedPolled,
    baseline_then,
    bind_own_login,
    event,
    install_oauth_capa,
    oauth_login,
    owned_polled_setup,
    polled_setup,
    register,
    use_kek,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    use_kek(monkeypatch)


@pytest.fixture(autouse=True)
def _queue(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)


@pytest.fixture(autouse=True)
def _own_pollers() -> Iterator[None]:
    saved = dict(sources._POLLERS)
    yield
    sources._POLLERS.clear()
    sources._POLLERS.update(saved)


async def _create(
    app_session: AppSessionFactory,
    owned: OwnedPolled,
    *,
    created_by: uuid.UUID | None = None,
    acts_for_creator: bool = True,
    config: dict[str, Any] | None = None,
) -> m.Trigger:
    async with app_session(owned.tenant) as db:
        return await create_trigger(
            db,
            tenant_id=owned.tenant,
            agent_id=owned.agent_id,
            kind="event",
            task_text="Handle it",
            event_source=owned.source,
            event_type=owned.event_type,
            created_by_member_id=created_by or owned.member_id,
            acts_for_creator=acts_for_creator,
            event_target_credential_id=owned.credential_id,
            event_config=config,
        )


async def _trigger(
    app_session: AppSessionFactory, tenant: uuid.UUID, trigger_id: uuid.UUID
) -> m.Trigger:
    async with app_session(tenant) as db:
        row = await db.get(m.Trigger, trigger_id)
        assert row is not None
        return row


async def _cursor(
    app_session: AppSessionFactory, tenant: uuid.UUID, trigger_id: uuid.UUID
) -> m.EventSourceCursor:
    async with app_session(tenant) as db:
        row = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger_id)
        )
        assert row is not None
        return row


async def _make_due(
    app_session: AppSessionFactory, tenant: uuid.UUID, trigger_id: uuid.UUID
) -> None:
    async with app_session(tenant) as db:
        row = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger_id)
        )
        assert row is not None
        row.next_poll_at = None


async def _runs(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID
) -> list[m.AgentRun]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.AgentRun)
                    .where(m.AgentRun.agent_id == agent_id)
                    .order_by(m.AgentRun.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _publish_new_mission(app_session: AppSessionFactory, owned: OwnedPolled) -> None:
    """The owner changes what the agent does: every confirmation of it waits."""
    async with app_session(owned.tenant) as db:
        agent = await db.get(m.Agent, owned.agent_id)
        assert agent is not None
        agent.mission = f"a new mission {uuid.uuid4().hex[:6]}"
        await db.flush()
        await publish_version(db, agent)


# --- creating it -----------------------------------------------------------------------


async def test_the_owner_creates_an_event_trigger_on_their_own_login(
    app_session: AppSessionFactory,
) -> None:
    owned = await owned_polled_setup(app_session)

    t = await _create(app_session, owned)

    assert t.event_target_credential_id == owned.credential_id
    assert t.acts_for_creator is True
    assert t.access_split is not None and t.access_split["v"] == 1
    async with app_session(owned.tenant) as db:
        assert await trigger_confirmed(db, await _trigger(app_session, owned.tenant, t.id))


async def test_someone_else_cannot_create_it(app_session: AppSessionFactory) -> None:
    owned = await owned_polled_setup(app_session)
    other = await member(app_session, owned.tenant, f"{uuid.uuid4().hex[:8]}@acme.test")

    with pytest.raises(InvalidTriggerConfig, match="department owner's own login"):
        await _create(app_session, owned, created_by=other)


async def test_it_needs_acts_for_creator(app_session: AppSessionFactory) -> None:
    owned = await owned_polled_setup(app_session)

    with pytest.raises(InvalidTriggerConfig, match="acting for them"):
        await _create(app_session, owned, acts_for_creator=False)


async def test_the_owners_login_is_the_target_only_for_the_owner(
    app_session: AppSessionFactory,
) -> None:
    owned = await owned_polled_setup(app_session)
    other = await member(app_session, owned.tenant, f"{uuid.uuid4().hex[:8]}@acme.test")
    async with app_session(owned.tenant) as db:
        mine = await polled_event_target(
            db,
            agent_id=owned.agent_id,
            event_source=owned.source,
            event_type=owned.event_type,
            creator=owned.member_id,
        )
        assert mine == LoginTarget("credential", owned.credential_id)
        for creator in (other, None):
            with pytest.raises(PolledTargetMissing):
                await polled_event_target(
                    db,
                    agent_id=owned.agent_id,
                    event_source=owned.source,
                    event_type=owned.event_type,
                    creator=creator,
                )


# --- polling it ------------------------------------------------------------------------


async def test_an_owned_login_polls_and_fires_a_run_for_its_owner(
    app_session: AppSessionFactory,
) -> None:
    owned = await owned_polled_setup(app_session)
    owned.fake.respond = baseline_then(lambda _c: [event(1)])
    t = await _create(app_session, owned)

    await poll_tick()  # baseline
    assert await _runs(app_session, owned.tenant, owned.agent_id) == []
    await _make_due(app_session, owned.tenant, t.id)
    await poll_tick()

    (run,) = await _runs(app_session, owned.tenant, owned.agent_id)
    assert run.context["trigger_id"] == str(t.id)
    login_fields = owned.fake.calls[-1][0]
    assert login_fields["password"] == SECRET
    async with app_session(owned.tenant) as db:
        assert (await whose_run(db, run)).person == owned.subject


async def test_an_unconfirmed_trigger_is_not_polled_and_keeps_its_cursor(
    app_session: AppSessionFactory,
) -> None:
    """Ruling F7: what arrived while the owner's confirmation was pending is
    caught up once they confirm, `MAX_EVENTS_PER_TICK` a tick, from the
    cursor and baseline it had."""
    owned = await owned_polled_setup(app_session)
    burst = [event(n) for n in range(25)]
    pending = {"on": False}
    owned.fake.respond = baseline_then(lambda _c: burst if pending["on"] else [])
    t = await _create(app_session, owned)
    await poll_tick()  # baseline
    before = await _cursor(app_session, owned.tenant, t.id)
    assert before.cursor == {"pos": 5} and before.baseline_id is not None
    calls = len(owned.fake.calls)

    await _publish_new_mission(app_session, owned)
    pending["on"] = True
    await _make_due(app_session, owned.tenant, t.id)
    await poll_tick()

    assert len(owned.fake.calls) == calls  # not polled
    row = await _trigger(app_session, owned.tenant, t.id)
    assert row.enabled is True
    assert row.last_skip_reason == NEEDS_CONFIRMATION
    kept = await _cursor(app_session, owned.tenant, t.id)
    assert kept.cursor == before.cursor and kept.baseline_id == before.baseline_id
    assert kept.failures == 0 and kept.last_error is None
    assert kept.next_poll_at is not None and kept.next_poll_at > datetime.now(tz=UTC)
    assert await _runs(app_session, owned.tenant, owned.agent_id) == []

    async with app_session(owned.tenant) as db:
        live = await db.get(m.Trigger, t.id)
        assert live is not None
        await stamp_trigger(db, live, confirmed_by=owned.member_id)
    await _make_due(app_session, owned.tenant, t.id)
    await poll_tick()

    assert len(await _runs(app_session, owned.tenant, owned.agent_id)) == MAX_EVENTS_PER_TICK
    mid = await _cursor(app_session, owned.tenant, t.id)
    assert mid.cursor == {"pos": 5} and mid.baseline_id == before.baseline_id
    assert (await _trigger(app_session, owned.tenant, t.id)).last_skip_reason is None

    await _make_due(app_session, owned.tenant, t.id)
    await poll_tick()

    assert len(await _runs(app_session, owned.tenant, owned.agent_id)) == 25
    after = await _cursor(app_session, owned.tenant, t.id)
    assert after.cursor == {"pos": 30} and after.baseline_id == before.baseline_id


async def test_the_owners_confirmation_lets_the_next_tick_fire(
    app_session: AppSessionFactory,
) -> None:
    """Task 9: the owner confirms through `POST /personal-department/confirm`
    and the next tick polls again and catches up what waited."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.auth import get_identity_provider
    from oc8.main import create_app

    owned = await owned_polled_setup(app_session)
    pending = {"on": False}
    owned.fake.respond = baseline_then(lambda _c: [event(1)] if pending["on"] else [])
    t = await _create(app_session, owned)
    await poll_tick()  # baseline
    await _publish_new_mission(app_session, owned)
    pending["on"] = True
    await _make_due(app_session, owned.tenant, t.id)
    await poll_tick()
    assert (await _trigger(app_session, owned.tenant, t.id)).last_skip_reason == NEEDS_CONFIRMATION
    assert await _runs(app_session, owned.tenant, owned.agent_id) == []

    token = get_identity_provider().mint(
        tenant_id=owned.tenant, subject=owned.subject, role="member"
    )
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/personal-department/confirmation", headers=headers)
            assert r.status_code == 200, r.text
            summary = r.json()
            assert [a["agentId"] for a in summary["agents"]] == [str(owned.agent_id)]
            r = await c.post(
                "/api/v1/personal-department/confirm",
                json={"token": summary["token"]},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["needed"] is False

    await _make_due(app_session, owned.tenant, t.id)
    await poll_tick()

    (run,) = await _runs(app_session, owned.tenant, owned.agent_id)
    assert run.context["trigger_id"] == str(t.id)
    assert (await _trigger(app_session, owned.tenant, t.id)).last_skip_reason is None


async def test_a_pending_confirmation_backs_off_and_never_switches_the_trigger_off(
    app_session: AppSessionFactory,
) -> None:
    """Ruling F13: a confirmation that went away waits for the owner; the
    trigger stays on and is tried again a tick later."""
    owned = await owned_polled_setup(app_session)
    t = await _create(app_session, owned)
    await poll_tick()
    await _publish_new_mission(app_session, owned)
    for _ in range(3):
        await _make_due(app_session, owned.tenant, t.id)
        await poll_tick()

    row = await _trigger(app_session, owned.tenant, t.id)
    assert row.enabled is True and row.last_skip_reason == NEEDS_CONFIRMATION
    assert (await _cursor(app_session, owned.tenant, t.id)).failures == 0


async def test_re_saving_the_binding_keeps_an_owned_trigger_enabled(
    app_session: AppSessionFactory,
) -> None:
    """Ruling F12."""
    owned = await owned_polled_setup(app_session)
    t = await _create(app_session, owned)

    await bind_own_login(
        app_session,
        owned.tenant,
        agent_id=owned.agent_id,
        member_id=owned.member_id,
        tool_key=owned.tool_key,
        credential_id=owned.credential_id,
    )

    row = await _trigger(app_session, owned.tenant, t.id)
    assert row.enabled is True and row.last_skip_reason is None


async def test_turning_acts_for_creator_off_switches_the_trigger_off(
    app_session: AppSessionFactory,
) -> None:
    owned = await owned_polled_setup(app_session)
    t = await _create(app_session, owned)
    async with app_session(owned.tenant) as db:
        live = await db.get(m.Trigger, t.id)
        assert live is not None
        live.acts_for_creator = False

    await poll_tick()

    assert owned.fake.calls == []
    row = await _trigger(app_session, owned.tenant, t.id)
    assert row.enabled is False and row.last_skip_reason == EVENT_TARGET_CHANGED


async def test_re_enabling_it_rechecks_the_owner(app_session: AppSessionFactory) -> None:
    owned = await owned_polled_setup(app_session)
    t = await _create(app_session, owned)
    async with app_session(owned.tenant) as db:
        live = await db.get(m.Trigger, t.id)
        assert live is not None
        await update_trigger(db, live, enabled=False)
    async with app_session(owned.tenant) as db:
        live = await db.get(m.Trigger, t.id)
        assert live is not None
        await update_trigger(db, live, enabled=True)
    assert (await _trigger(app_session, owned.tenant, t.id)).enabled is True

    other = await member(app_session, owned.tenant, f"{uuid.uuid4().hex[:8]}@acme.test")
    async with app_session(owned.tenant) as db:
        live = await db.get(m.Trigger, t.id)
        assert live is not None
        live.enabled = False
        live.created_by_member_id = other
    with pytest.raises(InvalidTriggerConfig, match="organisation login"):
        async with app_session(owned.tenant) as db:
            live = await db.get(m.Trigger, t.id)
            assert live is not None
            await update_trigger(db, live, enabled=True)


async def test_deleting_the_owned_login_switches_the_personal_trigger_off(
    app_session: AppSessionFactory,
) -> None:
    owned = await owned_polled_setup(app_session)
    t = await _create(app_session, owned)

    async with app_session(owned.tenant) as db:
        await delete_credential(
            db, tenant_id=owned.tenant, credential_id=owned.credential_id, force=True
        )
    await poll_tick()

    assert owned.fake.calls == []
    row = await _trigger(app_session, owned.tenant, t.id)
    assert row.enabled is False and row.last_skip_reason == EVENT_TARGET_CHANGED
    # Switched off, not waiting: a gone login only takes away (T12-N2).
    async with app_session(owned.tenant) as db:
        assert await trigger_confirmed(db, row)


async def test_a_departed_owners_login_is_never_polled(app_session: AppSessionFactory) -> None:
    owned = await owned_polled_setup(app_session)
    t = await _create(app_session, owned)
    async with app_session(owned.tenant) as db:
        # Soft-deleted without the offboarding hook: the poll itself refuses.
        gone = await db.get(m.OrgMember, owned.member_id)
        assert gone is not None
        gone.deleted_at = datetime.now(tz=UTC)

    await poll_tick()

    assert owned.fake.calls == []
    row = await _trigger(app_session, owned.tenant, t.id)
    assert row.enabled is False and row.last_skip_reason == EVENT_TARGET_CHANGED


# --- its settings and its token --------------------------------------------------------


async def _owned_oauth(
    app_session: AppSessionFactory,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str, str]:
    """An owned world whose agent is fixed, by its owner, to the owner's own
    OAuth connection of the OAuth test capa; and a tenant-wide setup row of
    that capa with a default mailbox. (tenant, member, agent, connection,
    source, tool_key)"""
    source = f"oo_{uuid.uuid4().hex[:10]}"
    tool_key = f"{source}_tools"
    world = await owned_world(
        app_session, tools={tool_key: {"enabled": True, "read": True, "modify": True}}
    )
    assert world.agent_id is not None
    await install_oauth_capa(app_session, world.tenant, source, tool_key)
    conn = await oauth_login(
        app_session,
        world.tenant,
        grant_type="authorization_code",
        owner=world.member_id,
        account_label="ada@acme.test",
    )
    async with app_session(world.tenant) as db:
        db.add(
            m.McpConnection(
                tenant_id=world.tenant,
                name=tool_key,
                server_url="",
                transport="stdio",
                config={
                    "_plugin_name": source,
                    "_connection_key": "default",
                    "env": {"TEST_DEFAULT_MAILBOX": "info@acme.test"},
                },
            )
        )
    await bind_own_login(
        app_session,
        world.tenant,
        agent_id=world.agent_id,
        member_id=world.member_id,
        tool_key=tool_key,
        oauth_connection_id=conn,
    )
    return world.tenant, world.member_id, world.agent_id, conn, source, tool_key


async def test_an_owned_login_never_reads_the_organisations_setup(
    app_session: AppSessionFactory,
) -> None:
    tenant, member_id, agent_id, conn, source, _tool_key = await _owned_oauth(app_session)
    org_conn = await oauth_login(app_session, tenant)
    async with app_session(tenant) as db:
        spec = await polled_event_spec(db, source, "item.arrived")
        assert spec is not None
        own = await connection_setup_value(
            db,
            spec,
            agent_id=agent_id,
            target=LoginTarget("oauth", conn),
            key="default_mailbox",
        )
        org = await connection_setup_value(
            db,
            spec,
            agent_id=agent_id,
            target=LoginTarget("oauth", org_conn),
            key="default_mailbox",
        )
    assert own == ""
    assert org == "info@acme.test"  # the same row fills an organisation login's

    fake = FakePoller(baseline_then(lambda _c: []))
    sources.register_poller(source, "item.arrived", fake)
    async with app_session(tenant) as db:
        t = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="event",
            task_text="Handle it",
            event_source=source,
            event_type="item.arrived",
            created_by_member_id=member_id,
            acts_for_creator=True,
            event_target_oauth_connection_id=conn,
        )
        assert t.event_config["mailbox"] == ""


async def test_an_owned_oauth_connection_gets_its_token(app_session: AppSessionFactory) -> None:
    tenant, member_id, agent_id, conn, source, _tool_key = await _owned_oauth(app_session)
    fake = FakePoller(baseline_then(lambda _c: [event(1)]))
    sources.register_poller(source, "item.arrived", fake)
    async with app_session(tenant) as db:
        t = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="event",
            task_text="Handle it",
            event_source=source,
            event_type="item.arrived",
            created_by_member_id=member_id,
            acts_for_creator=True,
            event_target_oauth_connection_id=conn,
        )

    await poll_tick()
    await _make_due(app_session, tenant, t.id)
    await poll_tick()

    assert fake.calls, "the owned connection was never polled"
    login_fields = fake.calls[-1][0]
    assert login_fields["access_token"] == TOKEN
    assert login_fields["account_label"] == "ada@acme.test"
    assert len(await _runs(app_session, tenant, agent_id)) == 1
    cursor = await _cursor(app_session, tenant, t.id)
    assert cursor.last_error is None


# --- an organisation login, as before --------------------------------------------------


async def test_an_organisation_login_trigger_behaves_as_before(
    app_session: AppSessionFactory,
) -> None:
    """Regression: a standard department's trigger on an organisation login
    needs neither `acts_for_creator` nor a confirmation to be polled, and is
    not held when its agent changes."""
    polled = await polled_setup(app_session)
    fake = FakePoller(baseline_then(lambda _c: [event(1)]))
    register(polled, fake)
    someone = await member(app_session, polled.tenant, f"{uuid.uuid4().hex[:8]}@acme.test")
    async with app_session(polled.tenant) as db:
        assert await polled_event_target(
            db,
            agent_id=polled.agent_id,
            event_source=polled.source,
            event_type=polled.event_type,
            creator=someone,
        ) == LoginTarget("credential", polled.credential_id)
        t = await create_trigger(
            db,
            tenant_id=polled.tenant,
            agent_id=polled.agent_id,
            kind="event",
            task_text="Handle it",
            event_source=polled.source,
            event_type=polled.event_type,
            created_by_member_id=someone,
            event_target_credential_id=polled.credential_id,
        )
    async with app_session(polled.tenant) as db:
        agent = await db.get(m.Agent, polled.agent_id)
        assert agent is not None
        agent.mission = "something else entirely"

    await poll_tick()
    await _make_due(app_session, polled.tenant, t.id)
    await poll_tick()

    assert len(await _runs(app_session, polled.tenant, polled.agent_id)) == 1
    row = await _trigger(app_session, polled.tenant, t.id)
    assert row.enabled is True and row.last_skip_reason is None


# --- review follow-ups (Task 6 -> Task 8) ----------------------------------------------


async def test_the_poll_signs_in_for_the_triggers_creator_not_the_logins_owner(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whose own login the poll may sign in with is the trigger's listening
    creator (`listening_creator`), never the login's owner read back from
    the login itself, which would only compare the login with itself."""
    polled = await polled_setup(app_session)
    register(polled, FakePoller(baseline_then(lambda _c: [])))
    someone = await member(app_session, polled.tenant, f"{uuid.uuid4().hex[:8]}@acme.test")
    async with app_session(polled.tenant) as db:
        await create_trigger(
            db,
            tenant_id=polled.tenant,
            agent_id=polled.agent_id,
            kind="event",
            task_text="Handle it",
            event_source=polled.source,
            event_type=polled.event_type,
            created_by_member_id=someone,
            acts_for_creator=True,
            event_target_credential_id=polled.credential_id,
        )
    seen: list[uuid.UUID | None] = []
    original = sources._login_fields

    async def spy(*args: Any, owner: uuid.UUID | None = None, **kwargs: Any) -> Any:
        seen.append(owner)
        return await original(*args, owner=owner, **kwargs)

    monkeypatch.setattr(sources, "_login_fields", spy)

    await poll_tick()

    assert seen == [someone]


async def test_an_owned_login_the_owner_did_not_bind_is_no_fixed_login(
    app_session: AppSessionFactory,
) -> None:
    """The owner's own login counts for their trigger only when they bound
    it themselves (`set_by_member_id`), as for their confirmation
    (`connections.confirmation._owner_binding`)."""
    owned = await owned_polled_setup(app_session)
    async with app_session(owned.tenant) as db:
        assert await sources.fixed_login(
            db, owned.agent_id, owned.tool_key, creator=owned.member_id
        ) == LoginTarget("credential", owned.credential_id)
        row = await db.scalar(
            select(m.AgentToolAccess).where(
                m.AgentToolAccess.agent_id == owned.agent_id,
                m.AgentToolAccess.tool_key == owned.tool_key,
            )
        )
        assert row is not None
        row.set_by_member_id = None
        await db.flush()
        assert (
            await sources.fixed_login(db, owned.agent_id, owned.tool_key, creator=owned.member_id)
            is None
        )


async def test_an_archived_agents_trigger_is_never_polled(app_session: AppSessionFactory) -> None:
    owned = await owned_polled_setup(app_session)
    t = await _create(app_session, owned)
    async with app_session(owned.tenant) as db:
        agent = await db.get(m.Agent, owned.agent_id)
        assert agent is not None
        agent.deleted_at = datetime.now(tz=UTC)

    await poll_tick()

    assert owned.fake.calls == []
    row = await _trigger(app_session, owned.tenant, t.id)
    assert row.enabled is False and row.last_skip_reason == AGENT_ARCHIVED
