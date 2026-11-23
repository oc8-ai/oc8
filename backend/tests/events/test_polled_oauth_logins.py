"""A polled trigger listens on the agent's fixed organisation login of either
kind: a `Credential` or an `OAuthConnection`. For an OAuth login core hands
the poller a fresh access token (delegated to the trigger's mailbox when the
login delegates), keeps that token out of every error, reports a login that
needs signing in again, and switches the trigger off once the login is gone
or no longer the agent's. A setting with `allowed_from` only takes a value
the setup allows, on save and again before every poll."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from oc8.events import sources
from oc8.events.sources import (
    EVENT_TARGET_CHANGED,
    LoginTarget,
    PollResult,
    poll_tick,
    register_poller,
)
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
from tests.events.polled import (
    DELEGATED_SCOPE,
    TOKEN,
    FakePoller,
    OAuthPolled,
    baseline_then,
    event,
    oauth_login,
    oauth_polled_setup,
    register_oauth,
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


@pytest.fixture
def minted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Delegated tokens are minted by core's own `mint_delegated_token`;
    here it answers without a provider and records what it was asked."""
    calls: list[dict[str, Any]] = []

    async def mint(db: Any, **kwargs: Any) -> str:
        calls.append(kwargs)
        return f"delegated-{kwargs['subject']}-token-xyz"

    monkeypatch.setattr("oc8.oauth.tokens.mint_delegated_token", mint)
    return calls


async def _trigger(
    app_session: AppSessionFactory, polled: OAuthPolled, config: dict[str, Any] | None = None
) -> uuid.UUID:
    async with app_session(polled.tenant) as db:
        target = await polled_event_target(
            db, agent_id=polled.agent_id, event_source=polled.source, event_type=polled.event_type
        )
        assert target == LoginTarget("oauth", polled.connection_id)
        trigger = await create_trigger(
            db,
            tenant_id=polled.tenant,
            agent_id=polled.agent_id,
            kind="event",
            task_text="Handle it",
            event_source=polled.source,
            event_type=polled.event_type,
            event_target_oauth_connection_id=polled.connection_id,
            event_config=config,
        )
        return trigger.id


async def _row(app_session: AppSessionFactory, tenant: uuid.UUID, trigger_id: uuid.UUID) -> Any:
    async with app_session(tenant) as db:
        trigger = await db.get(m.Trigger, trigger_id)
        cursor = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger_id)
        )
        return trigger, cursor


async def _due(app_session: AppSessionFactory, tenant: uuid.UUID, trigger_id: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        row = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger_id)
        )
        assert row is not None
        row.next_poll_at = None


# --- the target -----------------------------------------------------------------------------


async def test_a_trigger_listens_on_the_agents_fixed_oauth_login(
    app_session: AppSessionFactory, minted: list[dict[str, Any]]
) -> None:
    polled = await oauth_polled_setup(app_session)
    register_oauth(polled, FakePoller(baseline_then(lambda _c: [])))
    trigger_id = await _trigger(app_session, polled)
    trigger, _cursor = await _row(app_session, polled.tenant, trigger_id)
    assert trigger.event_target_oauth_connection_id == polled.connection_id
    assert trigger.event_target_credential_id is None
    # Filled from the setup's default mailbox.
    assert trigger.event_config == {"mailbox": "info@acme.test", "folder": "in"}


async def test_a_personal_oauth_connection_is_never_a_target(
    app_session: AppSessionFactory,
) -> None:
    polled = await oauth_polled_setup(app_session)
    owner = await member(app_session, polled.tenant, f"{uuid.uuid4().hex[:8]}@acme.test")
    personal = await oauth_login(
        app_session, polled.tenant, owner=owner, account_label="anna@acme.test"
    )
    async with app_session(polled.tenant) as db:
        access = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == polled.agent_id)
        )
        assert access is not None
        access.oauth_connection_id = personal
    async with app_session(polled.tenant) as db:
        with pytest.raises(PolledTargetMissing):
            await polled_event_target(
                db,
                agent_id=polled.agent_id,
                event_source=polled.source,
                event_type=polled.event_type,
            )
        with pytest.raises(InvalidTriggerConfig, match="organisation"):
            await create_trigger(
                db,
                tenant_id=polled.tenant,
                agent_id=polled.agent_id,
                kind="event",
                task_text="x",
                event_source=polled.source,
                event_type=polled.event_type,
                event_target_oauth_connection_id=personal,
            )


async def test_another_organisation_connection_than_the_fixed_one_is_refused(
    app_session: AppSessionFactory,
) -> None:
    polled = await oauth_polled_setup(app_session)
    other = await oauth_login(app_session, polled.tenant, account_label="other@iam.test")
    async with app_session(polled.tenant) as db:
        with pytest.raises(InvalidTriggerConfig, match="fixed to"):
            await create_trigger(
                db,
                tenant_id=polled.tenant,
                agent_id=polled.agent_id,
                kind="event",
                task_text="x",
                event_source=polled.source,
                event_type=polled.event_type,
                event_target_oauth_connection_id=other,
            )


async def test_a_trigger_names_at_most_one_target(app_session: AppSessionFactory) -> None:
    polled = await oauth_polled_setup(app_session)
    async with app_session(polled.tenant) as db:
        with pytest.raises(InvalidTriggerConfig, match="one login"):
            await create_trigger(
                db,
                tenant_id=polled.tenant,
                agent_id=polled.agent_id,
                kind="event",
                task_text="x",
                event_source=polled.source,
                event_type=polled.event_type,
                event_target_oauth_connection_id=polled.connection_id,
                event_target_credential_id=uuid.uuid4(),
            )


async def test_the_database_refuses_two_targets(app_session: AppSessionFactory) -> None:
    polled = await oauth_polled_setup(app_session)
    trigger_id = await _trigger(app_session, polled)
    cred = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with app_session(polled.tenant) as db:
            db.add(
                m.Credential(
                    id=cred,
                    tenant_id=polled.tenant,
                    name="x",
                    credential_type="x",
                    field_values={},
                    secret_refs={},
                )
            )
            await db.flush()
            trigger = await db.get(m.Trigger, trigger_id)
            assert trigger is not None
            trigger.event_target_credential_id = cred
            await db.flush()


async def test_one_polled_trigger_per_agent_and_event_with_an_oauth_target(
    app_session: AppSessionFactory,
) -> None:
    polled = await oauth_polled_setup(app_session)
    await _trigger(app_session, polled)
    with pytest.raises(InvalidTriggerConfig, match="already"):
        await _trigger(app_session, polled)


# --- the poll ------------------------------------------------------------------------------


async def test_the_poller_gets_a_token_delegated_to_the_mailbox(
    app_session: AppSessionFactory, minted: list[dict[str, Any]]
) -> None:
    polled = await oauth_polled_setup(app_session)
    fake = FakePoller(baseline_then(lambda _c: []))
    register_oauth(polled, fake)
    await _trigger(app_session, polled, {"mailbox": "Sales@acme.test"})
    await poll_tick()
    [(login, config, _cursor)] = fake.calls
    assert config == {"mailbox": "Sales@acme.test", "folder": "in"}
    assert login["access_token"] == "delegated-Sales@acme.test-token-xyz"
    assert login["grant_type"] == "service_account"
    assert login["provider"] == "google"
    assert login["account_label"] == "bot@project.iam.example.test"
    assert login["allowed_mailbox"] == "info@acme.test,sales@acme.test"
    # Only this test's connection: a tick also polls what earlier tests left.
    [call] = [c for c in minted if c["connection_id"] == polled.connection_id]
    assert call["subject"] == "Sales@acme.test"
    assert call["scope"] == DELEGATED_SCOPE
    assert call["connection_id"] == polled.connection_id


async def test_an_app_login_gets_its_own_token(app_session: AppSessionFactory) -> None:
    config = [{"key": "mailbox", "label": "Mailbox", "default_from": "default_mailbox"}]
    polled = await oauth_polled_setup(
        app_session, provider="microsoft", grant_type="client_credentials", config=config
    )
    fake = FakePoller(baseline_then(lambda _c: []))
    register_oauth(polled, fake)
    await _trigger(app_session, polled)
    await poll_tick()
    [(login, _config, _cursor)] = fake.calls
    assert login["access_token"] == TOKEN
    assert login["azure_tenant_id"] == "contoso.onmicrosoft.com"
    assert login["grant_type"] == "client_credentials"


async def test_the_token_never_reaches_the_recorded_error(
    app_session: AppSessionFactory, minted: list[dict[str, Any]]
) -> None:
    polled = await oauth_polled_setup(app_session)

    def leak(login: dict[str, str], _config: dict[str, Any], _cursor: Any) -> PollResult:
        raise RuntimeError(f"refused Bearer {login['access_token']}")

    register_oauth(polled, FakePoller(leak))
    trigger_id = await _trigger(app_session, polled)
    await poll_tick()
    _trigger_row, cursor = await _row(app_session, polled.tenant, trigger_id)
    assert cursor.last_error is not None and "refused Bearer ***" in cursor.last_error
    assert "delegated-" not in cursor.last_error


@pytest.mark.parametrize("status", ["needs_reauth", "revoked"])
async def test_a_login_that_must_sign_in_again_is_a_poll_error(
    app_session: AppSessionFactory, minted: list[dict[str, Any]], status: str
) -> None:
    polled = await oauth_polled_setup(app_session)
    fake = FakePoller(baseline_then(lambda _c: []))
    register_oauth(polled, fake)
    trigger_id = await _trigger(app_session, polled)
    async with app_session(polled.tenant) as db:
        conn = await db.get(m.OAuthConnection, polled.connection_id)
        assert conn is not None
        conn.status = status
    await poll_tick()
    assert fake.calls == []
    trigger, cursor = await _row(app_session, polled.tenant, trigger_id)
    assert trigger.enabled
    assert cursor.failures == 1
    assert cursor.last_error is not None and "connected again" in cursor.last_error


async def test_revoking_the_login_switches_the_trigger_off(
    app_session: AppSessionFactory,
) -> None:
    from oc8.connections.revoke import revoke_oauth_connection

    polled = await oauth_polled_setup(app_session)
    register_oauth(polled, FakePoller(baseline_then(lambda _c: [])))
    trigger_id = await _trigger(app_session, polled)
    async with app_session(polled.tenant) as db:
        conn = await db.get(m.OAuthConnection, polled.connection_id)
        assert conn is not None
        await revoke_oauth_connection(db, conn)
    trigger, _cursor = await _row(app_session, polled.tenant, trigger_id)
    assert not trigger.enabled
    assert trigger.last_skip_reason == EVENT_TARGET_CHANGED
    assert trigger.event_target_oauth_connection_id is None


async def test_a_deleted_login_switches_the_trigger_off_on_the_next_tick(
    app_session: AppSessionFactory,
) -> None:
    polled = await oauth_polled_setup(app_session)
    fake = FakePoller(baseline_then(lambda _c: []))
    register_oauth(polled, fake)
    trigger_id = await _trigger(app_session, polled)
    async with app_session(polled.tenant) as db:
        conn = await db.get(m.OAuthConnection, polled.connection_id)
        await db.delete(conn)
    await poll_tick()
    assert fake.calls == []
    trigger, _cursor = await _row(app_session, polled.tenant, trigger_id)
    assert not trigger.enabled and trigger.last_skip_reason == EVENT_TARGET_CHANGED


async def test_switching_the_agents_access_switches_the_trigger_off(
    app_session: AppSessionFactory,
) -> None:
    polled = await oauth_polled_setup(app_session)
    fake = FakePoller(baseline_then(lambda _c: []))
    register_oauth(polled, fake)
    trigger_id = await _trigger(app_session, polled)
    other = await oauth_login(app_session, polled.tenant, account_label="other@iam.test")
    async with app_session(polled.tenant) as db:
        access = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == polled.agent_id)
        )
        assert access is not None
        access.oauth_connection_id = other
    await poll_tick()
    assert fake.calls == []
    trigger, _cursor = await _row(app_session, polled.tenant, trigger_id)
    assert not trigger.enabled and trigger.last_skip_reason == EVENT_TARGET_CHANGED


async def test_new_events_fire_from_an_oauth_login(
    app_session: AppSessionFactory, minted: list[dict[str, Any]]
) -> None:
    polled = await oauth_polled_setup(app_session)
    register_oauth(polled, FakePoller(baseline_then(lambda _c: [event(1)])))
    trigger_id = await _trigger(app_session, polled)
    await poll_tick()
    await _due(app_session, polled.tenant, trigger_id)
    await poll_tick()
    async with app_session(polled.tenant) as db:
        runs = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == polled.agent_id)))
            .scalars()
            .all()
        )
    assert len(runs) == 1


# --- allowed_from ------------------------------------------------------------------------


async def test_a_setting_outside_the_setups_list_is_refused_on_save(
    app_session: AppSessionFactory,
) -> None:
    polled = await oauth_polled_setup(app_session)
    with pytest.raises(InvalidTriggerConfig, match="delegated_mailboxes"):
        await _trigger(app_session, polled, {"mailbox": "ceo@acme.test"})
    trigger_id = await _trigger(app_session, polled, {"mailbox": "SALES@acme.test"})
    async with app_session(polled.tenant) as db:
        trigger = await db.get(m.Trigger, trigger_id)
        assert trigger is not None
        with pytest.raises(InvalidTriggerConfig, match="delegated_mailboxes"):
            await update_trigger(db, trigger, event_config={"mailbox": "ceo@acme.test"})


async def test_a_login_that_acts_as_someone_needs_a_list(app_session: AppSessionFactory) -> None:
    """The setting the login acts as (`acts_as`) is refused while the setup
    names nobody: an empty list never means "anyone in the domain"."""
    polled = await oauth_polled_setup(app_session, delegated="")
    with pytest.raises(InvalidTriggerConfig, match="delegated_mailboxes"):
        await _trigger(app_session, polled)


async def test_a_login_that_signs_in_as_one_account_needs_no_list(
    app_session: AppSessionFactory,
) -> None:
    """A login that does not delegate (an account signed in for the
    organisation) acts as nobody else: its poller keeps to that account, and
    the empty list refuses nothing here."""
    polled = await oauth_polled_setup(app_session, grant_type="authorization_code", delegated="")
    trigger_id = await _trigger(app_session, polled)
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, trigger_id)
        assert row is not None and row.event_target_oauth_connection_id == polled.connection_id


async def test_a_list_shrunk_after_saving_stops_the_poll(
    app_session: AppSessionFactory, minted: list[dict[str, Any]]
) -> None:
    polled = await oauth_polled_setup(app_session)
    fake = FakePoller(baseline_then(lambda _c: []))
    register_oauth(polled, fake)
    trigger_id = await _trigger(app_session, polled, {"mailbox": "sales@acme.test"})
    async with app_session(polled.tenant) as db:
        setup = await db.get(m.McpConnection, polled.setup_id)
        assert setup is not None
        setup.config = {
            **setup.config,
            "env": {**setup.config["env"], "TEST_DELEGATED_MAILBOXES": "info@acme.test"},
        }
    await poll_tick()
    assert fake.calls == []
    assert [c for c in minted if c["connection_id"] == polled.connection_id] == []
    _trigger_row, cursor = await _row(app_session, polled.tenant, trigger_id)
    assert cursor.last_error is not None
    assert "mailbox" in cursor.last_error and "delegated_mailboxes" in cursor.last_error


def test_a_setup_list_splits_like_the_tools_list() -> None:
    """One parser on both sides: commas, semicolons, spaces and newlines
    (the google capa's `mailbox_policy.mailbox_list` splits the same way)."""
    assert sources.setup_list("a@x; b@x") == ["a@x", "b@x"]
    assert sources.setup_list(" a@x,\nb@x\tc@x ;;") == ["a@x", "b@x", "c@x"]


def test_the_list_compares_ascii_case_only() -> None:
    """ASCII letters match in any case; nothing else is folded, so a value
    that only casefolds to a listed one ("STRAßE" -> "strasse") is refused."""
    from dataclasses import replace

    from oc8.capas.manifest import TriggerConfigField

    field = TriggerConfigField(key="mailbox", label="Mailbox", allowed_from="list")
    spec = replace(_spec_stub(), config=[field])
    allowed = {"mailbox": ["strasse@acme.test", "Info@Acme.test"]}
    assert sources.refused_setting(spec, {"mailbox": "INFO@acme.TEST"}, allowed) is None
    assert sources.refused_setting(spec, {"mailbox": "STRAßE@acme.test"}, allowed) is not None


def _spec_stub() -> sources.PolledEventSpec:
    import dataclasses

    values: dict[str, Any] = {}
    for f in dataclasses.fields(sources.PolledEventSpec):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        values[f.name] = None
    return sources.PolledEventSpec(**values)


async def test_a_refresh_never_holds_the_login_row_during_the_poll(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token is fetched in its own short session, committed before the
    poller runs: a refresh locks the connection row (`FOR UPDATE`), and the
    tools and other polls on that login must not wait for a far system."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    config = [{"key": "mailbox", "label": "Mailbox", "default_from": "default_mailbox"}]
    polled = await oauth_polled_setup(
        app_session, provider="microsoft", grant_type="client_credentials", config=config
    )
    async with app_session(polled.tenant) as db:
        conn = await db.get(m.OAuthConnection, polled.connection_id)
        assert conn is not None
        conn.expires_at = datetime.now(tz=UTC) - timedelta(minutes=5)  # due a refresh
    refreshed: list[uuid.UUID] = []

    async def remint(db: Any, conn: m.OAuthConnection) -> str:
        refreshed.append(conn.id)
        return "fresh-app-token"

    monkeypatch.setattr("oc8.oauth.tokens._mint_client_credentials", remint)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking(login: dict[str, str], _config: Any, _cursor: Any) -> PollResult:
        assert login["access_token"] == "fresh-app-token"
        entered.set()
        await release.wait()
        return PollResult(next_cursor={"pos": 0})

    register_poller(polled.source, polled.event_type, blocking)
    await _trigger(app_session, polled)
    tick = asyncio.create_task(poll_tick())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        async with app_session(polled.tenant) as db:
            locked = await db.scalar(
                text("SELECT id FROM oauth_connection WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": polled.connection_id},
            )
        assert locked == polled.connection_id
    finally:
        release.set()
        await tick
    assert refreshed == [polled.connection_id]


async def test_a_connection_of_another_organisation_is_never_a_target(
    app_session: AppSessionFactory,
) -> None:
    polled = await oauth_polled_setup(app_session)
    foreign = (await oauth_polled_setup(app_session)).connection_id  # another tenant's
    async with app_session(polled.tenant) as db:
        with pytest.raises(InvalidTriggerConfig):
            await create_trigger(
                db,
                tenant_id=polled.tenant,
                agent_id=polled.agent_id,
                kind="event",
                task_text="x",
                event_source=polled.source,
                event_type=polled.event_type,
                event_target_oauth_connection_id=foreign,
            )


async def test_the_list_is_read_from_the_logins_own_setup_only(
    app_session: AppSessionFactory,
) -> None:
    """`allowed_from` names whom this login may act as: only the setup that
    made this login says so, never a department or organisation-wide row."""
    polled = await oauth_polled_setup(app_session)
    async with app_session(polled.tenant) as db:
        own = await db.get(m.McpConnection, polled.setup_id)
        assert own is not None
        # Not this login's own row any more: a row for every department.
        own.config = {k: v for k, v in own.config.items() if k != "oauth_connection_id"}
    with pytest.raises(InvalidTriggerConfig, match="delegated_mailboxes"):
        await _trigger(app_session, polled, {"mailbox": "info@acme.test"})


@pytest.mark.parametrize(
    ("directory", "fresh"),
    [
        ("contoso.onmicrosoft.com", False),
        (" Contoso.OnMicrosoft.com ", False),
        ("fabrikam.onmicrosoft.com", True),
    ],
    ids=["same directory", "same directory, other spelling", "another directory"],
)
async def test_an_app_login_moved_to_another_directory_takes_a_fresh_baseline(
    app_session: AppSessionFactory, directory: str, fresh: bool
) -> None:
    """An app-only login is kept by its app id; submitted again for another
    directory, the same row now reaches other mailboxes."""
    import httpx

    from oc8.oauth import http as oauth_http
    from oc8.oauth.provisioning import _provision_microsoft

    polled = await oauth_polled_setup(
        app_session,
        provider="microsoft",
        grant_type="client_credentials",
        config=[{"key": "folder", "label": "Folder", "default": "in"}],
    )
    trigger_id = await _trigger(app_session, polled)
    await poll_tick()
    _, before = await _row(app_session, polled.tenant, trigger_id)
    assert before.cursor is not None

    def handler(request: httpx.Request) -> httpx.Response:
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "at1", "expires_in": 3600})
        return httpx.Response(200, json={"value": []})

    oauth_http.set_transport_override(httpx.MockTransport(handler))
    try:
        async with app_session(polled.tenant) as db:
            conn = await db.get(m.OAuthConnection, polled.connection_id)
            assert conn is not None
            await _provision_microsoft(
                db,
                polled.tenant,
                {
                    "azure_tenant_id": directory,
                    "client_id": conn.account_label,
                    "client_secret": "rotated",
                },
            )
    finally:
        oauth_http.set_transport_override(None)

    _, after = await _row(app_session, polled.tenant, trigger_id)
    assert (after.cursor is None) is fresh
