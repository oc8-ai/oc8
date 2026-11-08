"""Writing to and stopping a running run from the chat (spec §3.2)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.runtime.adapter import EchoRuntimeStub
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID, subject: str, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


async def _run(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    operator: str | None,
    state: str = "running",
) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        ctx = {"originating_operator": operator} if operator else {}
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="chat" if operator else "cron",
            state=state,
            context=ctx,
        )
        db.add(run)
        await db.flush()
        return run.id


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_operator_writes_and_another_member_cannot(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        ok = await c.post(
            f"/api/v1/runs/{run_id}/inputs", json={"text": "use EU"}, headers=_h(tenant, "alice")
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["authorName"] and ok.json()["consumedAt"] is None
        refused = await c.post(
            f"/api/v1/runs/{run_id}/inputs", json={"text": "x"}, headers=_h(tenant, "bob")
        )
        assert refused.status_code == 404
        assert (
            await c.get(f"/api/v1/runs/{run_id}/steering", headers=_h(tenant, "bob"))
        ).status_code == 404
        steering = (
            await c.get(f"/api/v1/runs/{run_id}/steering", headers=_h(tenant, "alice"))
        ).json()
        assert steering["mayWrite"] is True and steering["acceptsInputs"] is True
        assert steering["inputs"][0]["text"] == "use EU"
        assert steering["inputs"][0]["consumedAt"] is None


async def test_an_unknown_run_is_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _run(app_session, tenant, operator="alice")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            f"/api/v1/runs/{uuid.uuid4()}/inputs", json={"text": "x"}, headers=_h(tenant, "alice")
        )
        assert r.status_code == 404


async def test_terminal_run_is_409(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice", state="done")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            f"/api/v1/runs/{run_id}/inputs", json={"text": "late"}, headers=_h(tenant, "alice")
        )
        assert r.status_code == 409 and "already finished" in r.text
        steering = (
            await c.get(f"/api/v1/runs/{run_id}/steering", headers=_h(tenant, "alice"))
        ).json()
        assert steering["mayWrite"] is False
        stop = await c.post(f"/api/v1/runs/{run_id}/stop", headers=_h(tenant, "alice"))
        assert stop.status_code == 409


async def test_an_empty_or_overlong_message_is_422(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        for text in ("", "x" * 4001):
            r = await c.post(
                f"/api/v1/runs/{run_id}/inputs", json={"text": text}, headers=_h(tenant, "alice")
            )
            assert r.status_code == 422


async def test_a_runtime_that_takes_no_messages_says_so(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _echo(*_a: Any, **_kw: Any) -> EchoRuntimeStub:
        return EchoRuntimeStub()

    monkeypatch.setattr("oc8.api.v1.run_inputs.resolve_runtime", _echo)
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        steering = (
            await c.get(f"/api/v1/runs/{run_id}/steering", headers=_h(tenant, "alice"))
        ).json()
        assert steering["acceptsInputs"] is False
        r = await c.post(
            f"/api/v1/runs/{run_id}/inputs", json={"text": "x"}, headers=_h(tenant, "alice")
        )
        assert r.status_code == 409 and "doesn't take messages" in r.text
        # Stopping still works.
        stop = await c.post(f"/api/v1/runs/{run_id}/stop", headers=_h(tenant, "alice"))
        assert stop.status_code == 202


async def test_autonomous_run_takes_input_from_a_manager_only(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator=None)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        manager = await c.post(
            f"/api/v1/runs/{run_id}/inputs",
            json={"text": "skip ticket 4"},
            headers=_h(tenant, "admin"),
        )
        assert manager.status_code == 201
        member = await c.post(
            f"/api/v1/runs/{run_id}/inputs", json={"text": "x"}, headers=_h(tenant, "emp", "member")
        )
        assert member.status_code in (403, 404)
        stop = await c.post(f"/api/v1/runs/{run_id}/stop", headers=_h(tenant, "emp", "member"))
        assert stop.status_code in (403, 404)


async def test_stop_records_a_cancellation_for_the_operator(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        assert (
            await c.post(f"/api/v1/runs/{run_id}/stop", headers=_h(tenant, "bob"))
        ).status_code == 404
        r = await c.post(f"/api/v1/runs/{run_id}/stop", headers=_h(tenant, "alice"))
        assert r.status_code == 202 and r.json() == {"state": "running"}
        again = await c.post(f"/api/v1/runs/{run_id}/stop", headers=_h(tenant, "alice"))
        assert again.status_code == 202
    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.RunCancellation).where(m.RunCancellation.run_id == run_id)))
            .scalars()
            .all()
        )
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == "run.cancel"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1 and rows[0].cancellation_kind == "operator_interrupted"
    assert events and events[0].resource["door"] == "chat"
