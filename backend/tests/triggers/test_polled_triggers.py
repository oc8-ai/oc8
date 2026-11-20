"""A trigger on a polled event names the login it listens on: the organisation
login its agent is fixed to for the event capa's tools, and only that."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.connections.access_write import write_tool_access
from oc8.events.sources import EVENT_TARGET_CHANGED, PollResult
from oc8.triggers.service import InvalidTriggerConfig, create_trigger, update_trigger
from tests.conftest import AppSessionFactory
from tests.connections.helpers import member
from tests.events.polled import (
    CREDENTIAL_TYPE,
    FakePoller,
    Polled,
    agent,
    login,
    polled_setup,
    register,
    use_kek,
)

pytestmark = pytest.mark.asyncio


def _never(*_args: Any) -> PollResult:
    raise AssertionError("not polled in these tests")


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    use_kek(monkeypatch)


@pytest.fixture
async def polled(app_session: AppSessionFactory) -> Polled:
    setup = await polled_setup(app_session)
    register(setup, FakePoller(_never))
    return setup


async def _create(
    app_session: AppSessionFactory,
    polled: Polled,
    *,
    agent_id: uuid.UUID | None = None,
    target: uuid.UUID | None = None,
    config: dict[str, Any] | None = None,
) -> m.Trigger:
    async with app_session(polled.tenant) as db:
        return await create_trigger(
            db,
            tenant_id=polled.tenant,
            agent_id=agent_id or polled.agent_id,
            kind="event",
            task_text="Handle it",
            event_source=polled.source,
            event_type=polled.event_type,
            event_target_credential_id=target,
            event_config=config,
        )


async def test_create_stores_target_config_and_a_baseline_cursor(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(
        app_session, polled, target=polled.credential_id, config={"folder": "Inbox"}
    )

    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        assert row.event_target_credential_id == polled.credential_id
        assert row.event_config == {"folder": "Inbox"}
        cursor = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger.id)
        )
        assert cursor is not None and cursor.cursor is None


async def test_refused_without_a_target(app_session: AppSessionFactory, polled: Polled) -> None:
    with pytest.raises(InvalidTriggerConfig, match="login"):
        await _create(app_session, polled, target=None)


async def test_refused_for_a_personal_login(app_session: AppSessionFactory, polled: Polled) -> None:
    owner = await member(app_session, polled.tenant, f"{uuid.uuid4().hex[:8]}@acme.test")
    personal = await login(app_session, polled.tenant, owner=owner)
    other_agent = await agent(
        app_session, polled.tenant, tool_key=polled.tool_key, credential_id=personal
    )

    with pytest.raises(InvalidTriggerConfig, match="organisation"):
        await _create(app_session, polled, agent_id=other_agent, target=personal)


async def test_refused_for_a_login_of_another_type(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    wrong = await login(app_session, polled.tenant, credential_type="other_login")
    other_agent = await agent(
        app_session, polled.tenant, tool_key=polled.tool_key, credential_id=wrong
    )

    with pytest.raises(InvalidTriggerConfig, match=CREDENTIAL_TYPE):
        await _create(app_session, polled, agent_id=other_agent, target=wrong)


async def test_refused_for_a_login_the_agent_is_not_fixed_to(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    other = await login(app_session, polled.tenant, username="bert")
    user_mode = await agent(
        app_session, polled.tenant, tool_key=polled.tool_key, credential_id=None, mode="user"
    )

    with pytest.raises(InvalidTriggerConfig, match="fixed"):
        await _create(app_session, polled, target=other)
    with pytest.raises(InvalidTriggerConfig, match="fixed"):
        await _create(app_session, polled, agent_id=user_mode, target=polled.credential_id)


async def test_refused_for_a_second_polled_trigger_of_the_type(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    await _create(app_session, polled, target=polled.credential_id)

    with pytest.raises(InvalidTriggerConfig, match="already"):
        await _create(app_session, polled, target=polled.credential_id)


async def test_a_pushed_event_takes_no_target(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    async with app_session(polled.tenant) as db:
        with pytest.raises(InvalidTriggerConfig):
            await create_trigger(
                db,
                tenant_id=polled.tenant,
                agent_id=polled.agent_id,
                kind="event",
                task_text="t",
                event_source=polled.source,
                event_type="pushed.thing",
                event_target_credential_id=polled.credential_id,
            )
        pushed = await create_trigger(
            db,
            tenant_id=polled.tenant,
            agent_id=polled.agent_id,
            kind="event",
            task_text="t",
            event_source=polled.source,
            event_type="pushed.thing",
        )
        assert pushed.event_target_credential_id is None


async def _write_access(
    app_session: AppSessionFactory,
    polled: Polled,
    *,
    tool_key: str,
    mode: str,
    credential_id: uuid.UUID | None,
) -> None:
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Agent, polled.agent_id)
        assert row is not None
        await write_tool_access(
            db,
            agent=row,
            tool_key=tool_key,
            mode=mode,
            oauth_connection_id=None,
            credential_id=credential_id,
            set_by_member_id=None,
            set_with_manage=True,
            actor_type="system",
            actor_id=None,
        )


async def _state(
    app_session: AppSessionFactory, polled: Polled, trigger_id: uuid.UUID
) -> m.Trigger:
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger_id)
        assert row is not None
        return row


async def test_changing_the_agents_fixed_login_switches_the_trigger_off(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, target=polled.credential_id)

    # The same login again, and another tool's login, leave it alone.
    await _write_access(
        app_session,
        polled,
        tool_key=polled.tool_key,
        mode="fixed",
        credential_id=polled.credential_id,
    )
    await _write_access(
        app_session, polled, tool_key="some_other_tool", mode="fixed", credential_id=None
    )
    assert (await _state(app_session, polled, trigger.id)).enabled is True

    other = await login(app_session, polled.tenant, username="bert")
    await _write_access(
        app_session, polled, tool_key=polled.tool_key, mode="fixed", credential_id=other
    )

    row = await _state(app_session, polled, trigger.id)
    assert row.enabled is False
    assert row.last_skip_reason == EVENT_TARGET_CHANGED


async def test_switching_the_tool_to_the_users_own_login_switches_it_off(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, target=polled.credential_id)

    await _write_access(
        app_session, polled, tool_key=polled.tool_key, mode="user", credential_id=None
    )

    assert (await _state(app_session, polled, trigger.id)).enabled is False


async def test_re_enabling_resets_the_cursor_and_rechecks_the_login(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, target=polled.credential_id)
    async with app_session(polled.tenant) as db:
        cursor = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger.id)
        )
        assert cursor is not None
        cursor.cursor = {"pos": 9}
        cursor.failures = 3

    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        await update_trigger(db, row, enabled=False)
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        await update_trigger(db, row, enabled=True)

    async with app_session(polled.tenant) as db:
        cursor = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger.id)
        )
        assert cursor is not None
        assert cursor.cursor is None and cursor.failures == 0

    other = await login(app_session, polled.tenant, username="bert")
    await _write_access(
        app_session, polled, tool_key=polled.tool_key, mode="fixed", credential_id=other
    )
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None and row.enabled is False
        with pytest.raises(InvalidTriggerConfig, match="fixed"):
            await update_trigger(db, row, enabled=True)


async def test_the_database_holds_one_polled_trigger_per_agent_and_event(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    from sqlalchemy.exc import IntegrityError

    await _create(app_session, polled, target=polled.credential_id)

    with pytest.raises(IntegrityError):
        async with app_session(polled.tenant) as db:
            db.add(
                m.Trigger(
                    tenant_id=polled.tenant,
                    agent_id=polled.agent_id,
                    kind="event",
                    task_text="again",
                    event_source=polled.source,
                    event_type=polled.event_type,
                    event_target_credential_id=polled.credential_id,
                )
            )


async def test_two_creates_at_once_leave_one_and_refuse_the_other_cleanly(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    import asyncio

    async with app_session(polled.tenant) as first:
        await create_trigger(
            first,
            tenant_id=polled.tenant,
            agent_id=polled.agent_id,
            kind="event",
            task_text="first",
            event_source=polled.source,
            event_type=polled.event_type,
            event_target_credential_id=polled.credential_id,
        )
        # The second create cannot see the first, uncommitted one; the unique
        # index makes it wait for the commit below and then refuses it.
        second = asyncio.create_task(_create(app_session, polled, target=polled.credential_id))
        await asyncio.sleep(0.5)
        assert not second.done()
    with pytest.raises(InvalidTriggerConfig, match="already"):
        await second


async def test_re_enabling_while_the_capa_is_off_says_so(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, target=polled.credential_id)
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        await update_trigger(db, row, enabled=False)
        capa_id = await db.scalar(select(m.Capa.id).where(m.Capa.name == polled.source))
        installation = await db.scalar(
            select(m.CapaInstallation).where(m.CapaInstallation.capa_id == capa_id)
        )
        assert installation is not None
        installation.status = "disabled"

    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        with pytest.raises(InvalidTriggerConfig, match="capa is not enabled"):
            await update_trigger(db, row, enabled=True)


async def test_another_integrity_error_is_not_reported_as_a_duplicate(
    app_session: AppSessionFactory, polled: Polled, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    async def passes(*_args: Any, **_kwargs: Any) -> bool:
        return True

    # As if the target login were deleted between the check and the insert:
    # the foreign key refuses it, which is no duplicate.
    monkeypatch.setattr("oc8.triggers.service._check_polled_target", passes)
    with pytest.raises(IntegrityError):
        await _create(app_session, polled, target=uuid.uuid4())
