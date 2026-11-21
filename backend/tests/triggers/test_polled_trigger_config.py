"""A polled event declares the settings a trigger on it takes
(`TriggerDecl.config`): only those keys reach `event_config`, each a short
string, and a missing one is filled from its default or from the setup of the
connection its login belongs to. Changing them starts a fresh baseline."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.events.sources import LoginTarget, PollResult
from oc8.triggers.service import (
    InvalidTriggerConfig,
    PolledTargetMissing,
    create_trigger,
    polled_event_target,
    update_trigger,
)
from tests.conftest import AppSessionFactory
from tests.events.polled import (
    FakePoller,
    Polled,
    agent,
    login,
    polled_setup,
    register,
    use_kek,
)

pytestmark = pytest.mark.asyncio

CONFIG = [
    {"key": "folder", "label": "Folder", "default": "INBOX"},
    {"key": "user", "label": "Mailbox", "default_from": "default_user"},
]
SETUP = {
    "title": "Set up",
    "fields": [{"key": "default_user", "label": "Default user", "kind": "text"}],
    "mcp": {"name": "tools", "env_fields": {"TEST_DEFAULT_USER": "default_user"}},
}


def _never(*_args: Any) -> PollResult:
    raise AssertionError("not polled in these tests")


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    use_kek(monkeypatch)


@pytest.fixture
async def polled(app_session: AppSessionFactory) -> Polled:
    setup = await polled_setup(app_session, config=CONFIG, setup=SETUP)
    register(setup, FakePoller(_never))
    return setup


async def _connection(
    app_session: AppSessionFactory,
    polled: Polled,
    value: str,
    *,
    credential_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    plugin: str | None = None,
) -> None:
    async with app_session(polled.tenant) as db:
        db.add(
            m.McpConnection(
                tenant_id=polled.tenant,
                name="tools",
                server_url="",
                credential_id=credential_id,
                department_id=department_id,
                config={
                    "_plugin_name": plugin or polled.source,
                    "_connection_key": "default",
                    "env": {"TEST_DEFAULT_USER": value},
                },
            )
        )


async def _create(
    app_session: AppSessionFactory, polled: Polled, config: dict[str, Any] | None
) -> m.Trigger:
    async with app_session(polled.tenant) as db:
        return await create_trigger(
            db,
            tenant_id=polled.tenant,
            agent_id=polled.agent_id,
            kind="event",
            task_text="Handle it",
            event_source=polled.source,
            event_type=polled.event_type,
            event_target_credential_id=polled.credential_id,
            event_config=config,
        )


async def test_missing_settings_are_filled_from_their_defaults(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, None)

    assert trigger.event_config == {"folder": "INBOX", "user": ""}


async def test_a_given_setting_is_kept_and_an_empty_one_is_filled(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, {"folder": "  ", "user": "anna@example.test"})

    assert trigger.event_config == {"folder": "INBOX", "user": "anna@example.test"}


async def test_a_default_comes_from_the_logins_connection_setup(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Agent, polled.agent_id)
        assert row is not None
        department = row.department_id
    # Another capa's row and another department's row never count.
    await _connection(app_session, polled, "elsewhere@example.test", plugin="other_capa")
    await _connection(app_session, polled, "other@example.test", department_id=uuid.uuid4())
    await _connection(app_session, polled, "dept@example.test", department_id=department)

    trigger = await _create(app_session, polled, {"folder": "Archive"})

    assert trigger.event_config == {"folder": "Archive", "user": "dept@example.test"}


async def test_the_logins_own_connection_wins(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    await _connection(app_session, polled, "shared@example.test")
    await _connection(
        app_session, polled, "pinned@example.test", credential_id=polled.credential_id
    )

    trigger = await _create(app_session, polled, None)

    assert trigger.event_config["user"] == "pinned@example.test"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"label": "x"}, "label"),
        ({"folder": 7}, "folder"),
        ({"folder": ["INBOX"]}, "folder"),
        ({"folder": "x" * 201}, "200"),
    ],
)
async def test_an_undeclared_or_bad_setting_is_refused(
    app_session: AppSessionFactory, polled: Polled, config: dict[str, Any], message: str
) -> None:
    with pytest.raises(InvalidTriggerConfig, match=message):
        await _create(app_session, polled, config)


async def test_an_event_without_settings_takes_none(app_session: AppSessionFactory) -> None:
    polled = await polled_setup(app_session, config=[])
    register(polled, FakePoller(_never))

    with pytest.raises(InvalidTriggerConfig, match="folder"):
        await _create(app_session, polled, {"folder": "INBOX"})
    assert (await _create(app_session, polled, None)).event_config == {}


async def _cursor(app_session: AppSessionFactory, polled: Polled, trigger_id: uuid.UUID) -> Any:
    async with app_session(polled.tenant) as db:
        return await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger_id)
        )


async def _advance(app_session: AppSessionFactory, polled: Polled, trigger_id: uuid.UUID) -> None:
    async with app_session(polled.tenant) as db:
        row = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger_id)
        )
        assert row is not None
        row.cursor = {"pos": 9}
        row.failures = 2
        row.last_error = "TimeoutError: slow"


async def test_changing_the_settings_starts_a_fresh_baseline(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, {"folder": "INBOX", "user": "a@example.test"})
    await _advance(app_session, polled, trigger.id)

    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        # The same settings again leave the position alone.
        await update_trigger(db, row, event_config={"folder": "INBOX", "user": "a@example.test"})
    assert (await _cursor(app_session, polled, trigger.id)).cursor == {"pos": 9}

    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        await update_trigger(db, row, event_config={"folder": "Archive"})
        assert row.event_config == {"folder": "Archive", "user": ""}
    cursor = await _cursor(app_session, polled, trigger.id)
    assert cursor.cursor is None and cursor.failures == 0 and cursor.last_error is None


async def test_updating_refuses_a_bad_setting_and_settings_on_other_triggers(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, None)
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        with pytest.raises(InvalidTriggerConfig, match="nope"):
            await update_trigger(db, row, event_config={"nope": "x"})
        cron = await create_trigger(
            db,
            tenant_id=polled.tenant,
            agent_id=polled.agent_id,
            kind="cron",
            task_text="t",
            cron_expression="0 9 * * *",
        )
        with pytest.raises(InvalidTriggerConfig):
            await update_trigger(db, cron, event_config={"folder": "x"})


async def test_the_target_is_the_agents_fixed_login(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    async with app_session(polled.tenant) as db:
        assert (
            await polled_event_target(
                db, agent_id=polled.agent_id, event_source=polled.source, event_type="x"
            )
            is None
        )
        assert (
            await polled_event_target(
                db,
                agent_id=polled.agent_id,
                event_source=polled.source,
                event_type=polled.event_type,
            )
            == LoginTarget("credential", polled.credential_id)
        )


async def test_no_fixed_login_means_no_target(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    user_mode = await agent(
        app_session, polled.tenant, tool_key=polled.tool_key, credential_id=None, mode="user"
    )

    async with app_session(polled.tenant) as db:
        with pytest.raises(PolledTargetMissing, match="fixed"):
            await polled_event_target(
                db, agent_id=user_mode, event_source=polled.source, event_type=polled.event_type
            )


async def test_re_targeting_to_the_agents_new_login_starts_a_fresh_baseline(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    trigger = await _create(app_session, polled, None)
    await _advance(app_session, polled, trigger.id)
    other = await login(app_session, polled.tenant, username="bert")

    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        # Not the agent's login: refused, nothing changes.
        with pytest.raises(InvalidTriggerConfig, match="fixed"):
            await update_trigger(db, row, event_target_credential_id=other)
    async with app_session(polled.tenant) as db:
        access = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == polled.agent_id)
        )
        assert access is not None
        access.credential_id = other
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger.id)
        assert row is not None
        await update_trigger(db, row, event_target_credential_id=other)
        assert row.event_target_credential_id == other
    cursor = await _cursor(app_session, polled, trigger.id)
    assert cursor.cursor is None and cursor.failures == 0


async def test_a_default_never_comes_from_another_departments_row(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    await _connection(app_session, polled, "other@example.test", department_id=uuid.uuid4())

    trigger = await _create(app_session, polled, None)

    assert trigger.event_config["user"] == ""


async def test_the_lookup_refuses_a_setup_field_that_is_not_plain(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    """Defence in depth behind the manifest check: a secret-kind field is
    never read, even when a row holds a value under its env name."""
    from dataclasses import replace

    from oc8.events.sources import connection_setup_value, polled_event_spec

    await _connection(app_session, polled, "s3cret", credential_id=polled.credential_id)
    async with app_session(polled.tenant) as db:
        spec = await polled_event_spec(db, polled.source, polled.event_type)
        assert spec is not None
        assert spec.setup_kinds == {"default_user": "text"}
        args = {
            "agent_id": polled.agent_id,
            "target": LoginTarget("credential", polled.credential_id),
        }
        assert await connection_setup_value(db, spec, key="default_user", **args) == "s3cret"
        for kind in ("password", "credential", "department"):
            hidden = replace(spec, setup_kinds={"default_user": kind})
            assert await connection_setup_value(db, hidden, key="default_user", **args) == ""
        unknown = replace(spec, setup_kinds={})
        assert await connection_setup_value(db, unknown, key="default_user", **args) == ""


async def test_a_fresh_baseline_forgets_when_it_last_polled(
    app_session: AppSessionFactory, polled: Polled
) -> None:
    from datetime import UTC, datetime

    trigger = await _create(app_session, polled, None)
    async with app_session(polled.tenant) as db:
        row = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger.id)
        )
        assert row is not None
        row.last_polled_at = row.last_ok_at = datetime.now(tz=UTC)
    async with app_session(polled.tenant) as db:
        stored = await db.get(m.Trigger, trigger.id)
        assert stored is not None
        await update_trigger(db, stored, event_config={"folder": "Archive"})
    cursor = await _cursor(app_session, polled, trigger.id)
    assert cursor.last_polled_at is None and cursor.last_ok_at is None
