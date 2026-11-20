"""The trigger API for polled events: the editor lists the events with their
settings, creates a trigger on the agent's fixed login, changes its settings
and reads how polling goes."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.events.sources import EVENT_TARGET_CHANGED, PollResult
from oc8.main import create_app
from tests.conftest import AppSessionFactory
from tests.events.polled import (
    EVENT_TYPE,
    LABEL,
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


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _create(
    client: AsyncClient, polled: Polled, agent_id: uuid.UUID | None = None, **body: Any
) -> Any:
    return await client.post(
        f"/api/v1/agents/{agent_id or polled.agent_id}/triggers",
        json={
            "kind": "event",
            "taskText": "Sort the mail",
            "eventSource": polled.source,
            "eventType": EVENT_TYPE,
            **body,
        },
        headers=_headers(polled.tenant),
    )


async def test_the_editor_lists_polled_events_with_their_settings_and_tool(
    client: AsyncClient, polled: Polled
) -> None:
    resp = await client.get("/api/v1/trigger-events", headers=_headers(polled.tenant))

    assert resp.status_code == 200, resp.text
    events = {(e["source"], e["type"]): e for e in resp.json()}
    event = events[(polled.source, EVENT_TYPE)]
    assert event["label"] == LABEL
    assert event["polled"] is True
    assert event["toolKey"] == polled.tool_key
    assert event["editorLabel"] == "" and event["editorLabelTranslations"] == {}
    # Not on disk here: the capa's name stands in for its display name.
    assert event["sourceLabel"] == polled.source
    assert event["config"] == [
        {
            "key": "folder",
            "label": "Folder",
            "labelTranslations": {},
            "kind": "text",
            "default": "in",
            "defaultFrom": "",
        }
    ]
    pushed = events[(polled.source, "pushed.thing")]
    assert pushed["polled"] is False and pushed["toolKey"] is None and pushed["config"] == []


async def test_create_listens_on_the_agents_fixed_login(
    client: AsyncClient, app_session: AppSessionFactory, polled: Polled
) -> None:
    resp = await _create(client, polled, eventConfig={"folder": "Archive"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["eventTargetCredentialId"] == str(polled.credential_id)
    assert body["eventConfig"] == {"folder": "Archive"}
    assert body["pollStatus"] == {
        "lastPolledAt": None,
        "lastOkAt": None,
        "lastError": None,
        "lastErrorAt": None,
    }
    assert body["lastSkipReason"] is None
    async with app_session(polled.tenant) as db:
        row = await db.get(m.Trigger, uuid.UUID(body["id"]))
        assert row is not None and row.event_target_credential_id == polled.credential_id


async def test_create_refuses_an_agent_without_a_login_and_a_bad_setting(
    client: AsyncClient, app_session: AppSessionFactory, polled: Polled
) -> None:
    no_login = await agent(
        app_session, polled.tenant, tool_key=polled.tool_key, credential_id=None, mode="user"
    )

    resp = await _create(client, polled, agent_id=no_login)
    assert resp.status_code == 422
    assert "tool settings" in resp.json()["detail"]

    resp = await _create(client, polled, eventConfig={"subject": "x"})
    assert resp.status_code == 422
    assert "subject" in resp.json()["detail"]


async def test_the_poll_status_and_skip_reason_show_on_the_trigger(
    client: AsyncClient, app_session: AppSessionFactory, polled: Polled
) -> None:
    created = (await _create(client, polled)).json()
    failed_at = dt.datetime(2026, 11, 20, 10, 42, tzinfo=dt.UTC)
    async with app_session(polled.tenant) as db:
        cursor = await db.scalar(
            select(m.EventSourceCursor).where(
                m.EventSourceCursor.trigger_id == uuid.UUID(created["id"])
            )
        )
        assert cursor is not None
        cursor.last_polled_at = failed_at
        cursor.last_error = "LoginFailed: login failed"
        cursor.last_error_at = failed_at
        trigger = await db.get(m.Trigger, uuid.UUID(created["id"]))
        assert trigger is not None
        trigger.enabled = False
        trigger.last_skip_reason = EVENT_TARGET_CHANGED
        db.add(
            m.Trigger(
                tenant_id=polled.tenant,
                agent_id=polled.agent_id,
                kind="cron",
                task_text="daily",
                cron_expression="0 9 * * *",
            )
        )

    resp = await client.get(
        f"/api/v1/agents/{polled.agent_id}/triggers", headers=_headers(polled.tenant)
    )

    by_kind = {t["kind"]: t for t in resp.json()}
    status = by_kind["event"]["pollStatus"]
    assert status["lastError"] == "LoginFailed: login failed"
    assert dt.datetime.fromisoformat(status["lastErrorAt"]) == failed_at
    assert dt.datetime.fromisoformat(status["lastPolledAt"]) == failed_at
    assert status["lastOkAt"] is None
    assert by_kind["event"]["lastSkipReason"] == EVENT_TARGET_CHANGED
    assert by_kind["cron"]["pollStatus"] is None
    assert by_kind["cron"]["eventConfig"] == {}
    assert by_kind["cron"]["eventTargetCredentialId"] is None


async def test_update_changes_the_settings_and_starts_a_fresh_baseline(
    client: AsyncClient, app_session: AppSessionFactory, polled: Polled
) -> None:
    created = (await _create(client, polled)).json()
    trigger_id = uuid.UUID(created["id"])
    async with app_session(polled.tenant) as db:
        cursor = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger_id)
        )
        assert cursor is not None
        cursor.cursor = {"pos": 3}

    resp = await client.patch(
        f"/api/v1/triggers/{trigger_id}",
        json={
            "eventConfig": {"folder": "Archive"},
            "eventTargetCredentialId": str(polled.credential_id),
            "taskText": "File it",
        },
        headers=_headers(polled.tenant),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["eventConfig"] == {"folder": "Archive"}
    assert resp.json()["taskText"] == "File it"
    async with app_session(polled.tenant) as db:
        cursor = await db.scalar(
            select(m.EventSourceCursor).where(m.EventSourceCursor.trigger_id == trigger_id)
        )
        assert cursor is not None and cursor.cursor is None

    resp = await client.patch(
        f"/api/v1/triggers/{trigger_id}",
        json={"eventConfig": {"folder": "x" * 201}},
        headers=_headers(polled.tenant),
    )
    assert resp.status_code == 422


def _member_headers(tenant: uuid.UUID, subject: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_whitespace_alone_in_the_settings_changes_nothing(
    client: AsyncClient, polled: Polled
) -> None:
    created = (await _create(client, polled, eventConfig={"folder": "Orders"})).json()
    owner = created["createdByMemberId"]
    url = f"/api/v1/triggers/{created['id']}"
    other = _member_headers(polled.tenant, "someone-else")

    same = await client.patch(url, json={"eventConfig": {"folder": "  Orders "}}, headers=other)
    assert same.status_code == 200, same.text
    assert same.json()["createdByMemberId"] == owner

    changed = await client.patch(url, json={"eventConfig": {"folder": "Archive"}}, headers=other)
    assert changed.status_code == 200, changed.text
    assert changed.json()["createdByMemberId"] != owner


async def test_update_refuses_another_agents_login(
    client: AsyncClient, app_session: AppSessionFactory, polled: Polled
) -> None:
    created = (await _create(client, polled)).json()
    other_login = await login(app_session, polled.tenant, username="bert")
    await agent(app_session, polled.tenant, tool_key=polled.tool_key, credential_id=other_login)

    resp = await client.patch(
        f"/api/v1/triggers/{created['id']}",
        json={"eventTargetCredentialId": str(other_login)},
        headers=_headers(polled.tenant),
    )

    assert resp.status_code == 422
    assert "fixed" in resp.json()["detail"]
