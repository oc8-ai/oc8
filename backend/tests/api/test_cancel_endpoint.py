from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _queued_run(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as s:
        agent = m.Agent(
            tenant_id=tenant, department_id=uuid.uuid4(), name="Dev", status="running"
        )
        s.add(agent)
        await s.flush()
        run = await RunRepository(s).create(
            tenant_id=tenant, agent_id=agent.id, context={"task": "x"}
        )
        return run.id, agent.id


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_cancel_queued_run_records_and_worker_interrupts(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, agent_id = await _queued_run(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=_headers(tenant))
            assert r.status_code == 200
            # Cooperative: the run is still queued; the cancel is recorded and
            # applied by the executor at pickup, not synchronously here.
            assert r.json()["state"] == "queued"

    async with app_session(tenant) as s:
        cancel = (
            await s.execute(
                select(m.RunCancellation).where(m.RunCancellation.run_id == run_id)
            )
        ).scalar_one_or_none()
        assert cancel is not None and cancel.cancellation_kind == "operator_interrupted"
        audit = (
            await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == "run.cancel"))
        ).scalars().all()
        assert any(str(run_id) in str(e.resource) for e in audit)

    # Drive the worker once: the queued run is skipped before start and ends
    # interrupted, the agent freed, the runtime never invoked.
    async def must_not_run(db: Any, **kw: Any) -> Any:
        raise AssertionError("a cancelled queued run must not execute")

    class _R:
        async def execute(self, db: Any, **kw: Any) -> Any:
            return await must_not_run(db, **kw)

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_R(),
    )

    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value
        agent = await s.get(m.Agent, agent_id)
        assert agent is not None and agent.status == "idle"


async def test_cancel_running_run_records_the_signal(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _agent_id = await _queued_run(app_session, tenant)
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        await RunRepository(s).transition(run, RunState.RUNNING)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=_headers(tenant))
            assert r.status_code == 200
            # Still running -- the signal is recorded; the run stops cooperatively.
            assert r.json()["state"] == "running"

    async with app_session(tenant) as s:
        cancel = (
            await s.execute(
                select(m.RunCancellation).where(m.RunCancellation.run_id == run_id)
            )
        ).scalar_one_or_none()
        assert cancel is not None


async def test_cancel_is_idempotent(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _agent_id = await _queued_run(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r1 = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=_headers(tenant))
            r2 = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=_headers(tenant))
            assert r1.status_code == 200 and r2.status_code == 200

    async with app_session(tenant) as s:
        rows = (
            await s.execute(
                select(m.RunCancellation).where(m.RunCancellation.run_id == run_id)
            )
        ).scalars().all()
        assert len(rows) == 1  # second cancel added no duplicate row


async def test_cancel_terminal_run_conflicts(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _agent_id = await _queued_run(app_session, tenant)
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        repo = RunRepository(s)
        await repo.transition(run, RunState.RUNNING)
        await repo.transition(run, RunState.DONE)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=_headers(tenant))
            assert r.status_code == 409


async def test_cancel_unknown_run_404s(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/runs/{uuid.uuid4()}/cancel", headers=_headers(tenant)
            )
            assert r.status_code == 404


async def test_cancel_wakes_a_run_parked_on_an_approval(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run parked on an approval is not looping and no executor holds it, so
    the cancel re-queues it into the before-start check instead of leaving it
    parked until somebody decides a card nobody needs any more. Its own cards
    are expired: tests/runtime/test_stop_parked_run.py."""
    enqueued: list[uuid.UUID] = []

    class _Queue:
        async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
            enqueued.append(run_id)

    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", _Queue)
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _agent_id = await _queued_run(app_session, tenant)
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        repo = RunRepository(s)
        await repo.transition(run, RunState.RUNNING)
        await repo.transition(run, RunState.WAITING_FOR_APPROVAL)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=_headers(tenant))
            assert r.status_code == 200
            assert r.json()["state"] == "queued"

    assert enqueued == [run_id]
    async with app_session(tenant) as s:
        cancel = (
            await s.execute(
                select(m.RunCancellation).where(m.RunCancellation.run_id == run_id)
            )
        ).scalar_one_or_none()
        assert cancel is not None
