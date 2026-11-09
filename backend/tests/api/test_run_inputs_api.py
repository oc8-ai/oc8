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
from oc8.runtime.clarification import SECRET_REFUSAL
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
    source: str | None = None,
    context: dict[str, Any] | None = None,
    assistant: bool = False,
) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="A", is_tenant_assistant=assistant
        )
        db.add(agent)
        await db.flush()
        ctx = {"originating_operator": operator} if operator else {}
        ctx.update(context or {})
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source=source or ("chat" if operator else "cron"),
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

    monkeypatch.setattr("oc8.runtime.registry.resolve_runtime", _echo)
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
        assert member.status_code == 404
        stop = await c.post(f"/api/v1/runs/{run_id}/stop", headers=_h(tenant, "emp", "member"))
        assert stop.status_code == 404


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


async def _refused_everywhere(c: AsyncClient, run_id: uuid.UUID, h: dict[str, str]) -> None:
    """Inputs, steering and stop all answer "no such run"."""
    assert (
        await c.post(f"/api/v1/runs/{run_id}/inputs", json={"text": "x"}, headers=h)
    ).status_code == 404
    assert (await c.get(f"/api/v1/runs/{run_id}/steering", headers=h)).status_code == 404
    assert (await c.post(f"/api/v1/runs/{run_id}/stop", headers=h)).status_code == 404


async def test_an_admin_cannot_steer_a_colleagues_messenger_chat_turn(
    app_session: AppSessionFactory,
) -> None:
    """Ruling 54: a messenger turn (no operator, `source=chat`) is somebody's
    run, not the agent's own -- managing the agent does not open it."""
    tenant = uuid.uuid4()
    run_id = await _run(
        app_session,
        tenant,
        operator=None,
        source="chat",
        context={"chat_session_id": str(uuid.uuid4())},
    )
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _refused_everywhere(c, run_id, _h(tenant, "admin"))


async def test_the_owner_steers_and_stops_her_messenger_turn_and_an_admin_cannot(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Ruling 58: a messenger turn is its chat owner's run -- in the web chat
    she gets Steer and Stop for it; an admin (another member) still gets
    nothing."""
    from tests.channels.test_messenger_steering import messenger_turn

    tenant = uuid.uuid4()
    run_id = await messenger_turn(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _refused_everywhere(c, run_id, _h(tenant, "admin"))
        owner = _h(tenant, "pat")
        steering = await c.get(f"/api/v1/runs/{run_id}/steering", headers=owner)
        assert steering.status_code == 200, steering.text
        assert steering.json()["mayWrite"] is True
        ok = await c.post(
            f"/api/v1/runs/{run_id}/inputs", json={"text": "nur Berlin"}, headers=owner
        )
        assert ok.status_code == 201, ok.text
        stop = await c.post(f"/api/v1/runs/{run_id}/stop", headers=owner)
        assert stop.status_code == 202, stop.text


async def test_a_manager_cannot_steer_a_run_on_somebody_elses_behalf(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(
        app_session, tenant, operator=None, source="decision", context={"for_person": "alice"}
    )
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _refused_everywhere(c, run_id, _h(tenant, "admin"))
        # Her run: she steers it.
        ok = await c.post(
            f"/api/v1/runs/{run_id}/inputs", json={"text": "go on"}, headers=_h(tenant, "alice")
        )
        assert ok.status_code == 201, ok.text


async def test_the_assistants_secret_gate_covers_run_inputs(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice", assistant=True)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            f"/api/v1/runs/{run_id}/inputs",
            json={"text": "apiKey=SENTINEL-NEVER-STORED"},
            headers=_h(tenant, "alice"),
        )
        assert r.status_code == 422
        assert r.json()["detail"] == SECRET_REFUSAL
        fine = await c.post(
            f"/api/v1/runs/{run_id}/inputs", json={"text": "use EU"}, headers=_h(tenant, "alice")
        )
        assert fine.status_code == 201
    async with app_session(tenant) as db:
        texts = (
            (await db.execute(select(m.RunInput.text).where(m.RunInput.run_id == run_id)))
            .scalars()
            .all()
        )
    assert texts == ["use EU"]


async def test_an_ordinary_agents_run_input_is_not_secret_gated(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    run_id = await _run(app_session, tenant, operator="alice")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.post(
            f"/api/v1/runs/{run_id}/inputs",
            json={"text": "apiKey=rotate-it-now"},
            headers=_h(tenant, "alice"),
        )
        assert r.status_code == 201
