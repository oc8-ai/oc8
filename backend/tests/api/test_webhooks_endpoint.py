"""POST /webhooks/{token} -- the generic, n8n-Webhook-node-style ingress for
kind='webhook' Triggers. Unlike /events/{source} (test_events_endpoint.py,
if it exists -- GitHub-specific, HMAC-verified), this endpoint has no
per-source parsing at all: any JSON body from any caller fires the one
Trigger its token names."""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.tenants.activity import set_tenant_activity_gate
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _webhook_trigger(
    app_session: AppSessionFactory, tenant: uuid.UUID, task_text: str = "React to the webhook"
) -> tuple[uuid.UUID, str, dict[str, str]]:
    """Creates the agent + trigger through the real HTTP API (not a direct
    DB insert) so the token this test uses is the exact one create_trigger's
    secrets.token_urlsafe generates -- not a value the test made up.

    Also seeds an Organization row: POST /webhooks/{token} has no tenant
    context from the URL alone, so it discovers candidate tenants via
    list_active_tenant_ids() (triggers/scheduler.py), which reads
    Organization rows, not Agent/Trigger rows -- the same requirement
    tests/triggers/test_scheduler.py's own tenant fixture already documents.
    """
    async with app_session(tenant) as s:
        s.add(
            m.Organization(
                id=tenant,
                slug=f"test-{tenant.hex[:12]}",
                name="Test Org",
                tier="standard",
                region="eu",
            )
        )
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        agent_id = agent.id
    token_str = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    headers = {"Authorization": f"Bearer {token_str}"}
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "webhook", "taskText": task_text},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            token = r.json()["webhookUrl"].rsplit("/", 1)[-1]
    return agent_id, token, headers


async def test_posting_to_the_webhook_url_enqueues_a_run_with_the_payload(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    # redis_url must be REQUESTED, not merely available: enqueue_run publishes
    # to the run-intake stream, and a client created before this fixture binds
    # settings would default to the compose port (see conftest.py's own note).
    tenant = uuid.uuid4()
    agent_id, token, _ = await _webhook_trigger(app_session, tenant, task_text="Work the ticket")

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/webhooks/{token}",
                json={"_model": "helpdesk.ticket", "_id": 42, "name": "Printer is on fire"},
            )
            assert r.status_code == 202, r.text

    async with app_session(tenant) as s:
        runs = (
            (await s.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(runs) == 1
        task = runs[0].context["task"]
        assert "Work the ticket" in task
        assert "helpdesk.ticket" in task
        assert "Printer is on fire" in task
        assert runs[0].source == "webhook"


async def test_unknown_token_is_404(app_session: AppSessionFactory) -> None:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/webhooks/{uuid.uuid4().hex}", json={"anything": "goes"})
            assert r.status_code == 404, r.text


async def test_disabled_webhook_trigger_is_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, token, headers = await _webhook_trigger(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            listed = await c.get(f"/api/v1/agents/{agent_id}/triggers", headers=headers)
            trigger_id = next(t["id"] for t in listed.json() if t["kind"] == "webhook")
            disabled = await c.patch(
                f"/api/v1/triggers/{trigger_id}", json={"enabled": False}, headers=headers
            )
            assert disabled.status_code == 200, disabled.text

            r = await c.post(f"/api/v1/webhooks/{token}", json={"anything": "goes"})
            assert r.status_code == 404, r.text


async def test_non_json_body_does_not_crash_the_endpoint(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id, token, _ = await _webhook_trigger(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/webhooks/{token}",
                content=b"not json at all",
                headers={"Content-Type": "text/plain"},
            )
            assert r.status_code == 202, r.text

    async with app_session(tenant) as s:
        runs = (
            (await s.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(runs) == 1
        assert "not json at all" in runs[0].context["task"]


async def test_empty_body_still_fires(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    agent_id, token, _ = await _webhook_trigger(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/webhooks/{token}")
            assert r.status_code == 202, r.text

    async with app_session(tenant) as s:
        count = (
            (await s.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(count) == 1


async def test_webhook_of_an_inactive_organisation_is_not_found(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id, token, _ = await _webhook_trigger(app_session, tenant)

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    set_tenant_activity_gate(gate)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/webhooks/{token}", json={"anything": "goes"})
            assert r.status_code == 404, r.text
            assert r.json()["detail"] == "unknown or disabled webhook"

    async with app_session(tenant) as s:
        runs = (
            (await s.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert runs == []


async def test_a_webhook_asks_the_activity_gate_at_most_once_per_request(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The gate is asked only for the organisation whose trigger matches the
    token, not for every organisation on each unauthenticated request."""
    tenant = uuid.uuid4()
    _, token, _ = await _webhook_trigger(app_session, tenant)
    # A second organisation, so a loop that asked the gate for every
    # organisation would ask it more than once.
    await _webhook_trigger(app_session, uuid.uuid4())

    asked: list[uuid.UUID] = []
    may_run = {"answer": False}

    async def gate(tenant_id: uuid.UUID) -> bool:
        asked.append(tenant_id)
        return may_run["answer"]

    set_tenant_activity_gate(gate)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # Inactive: the lookup asks once, for the matching organisation.
            refused = await c.post(f"/api/v1/webhooks/{token}", json={"anything": "goes"})
            assert refused.status_code == 404, refused.text
            assert refused.json()["detail"] == "unknown or disabled webhook"
            assert asked == [tenant]

            # Unknown token: the gate is never asked.
            asked.clear()
            unknown = await c.post(f"/api/v1/webhooks/{uuid.uuid4().hex}", json={})
            assert unknown.status_code == 404, unknown.text
            assert asked == []

            # Active: once by the lookup, once more by enqueue_run as it fires.
            asked.clear()
            may_run["answer"] = True
            accepted = await c.post(f"/api/v1/webhooks/{token}", json={"anything": "goes"})
            assert accepted.status_code == 202, accepted.text
            assert asked == [tenant, tenant]
