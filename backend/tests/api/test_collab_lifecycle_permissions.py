"""Who may archive, cancel and delete what cross-department collaboration made.

Each new door is gated like the create door of the same thing. Cancelling a
flow run rejects handoffs in other departments and stops their agent runs, so
it takes `flow:manage` (never delegatable), not the delegatable `run:control`:
an operator or a composed role holding `run:control` must not reach work
outside its own seats through it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import FLOW, RUN_CONTROL, RUN_START, VIEW, perm
from oc8.authz.scope import subject_uuid_for
from oc8.collab.handoff import create_handoff_type
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def _setup(app_session: AppSessionFactory) -> dict[str, str]:
    """A tenant with a published flow, a running run, a handoff type and a
    binding, plus a member holding the composed role run:start + run:control +
    flow:view."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        src = m.Department(tenant_id=tenant, name="Support", frame={"emits": ["x.done"]})
        tgt = m.Department(
            tenant_id=tenant,
            name="Finance",
            frame={"intakes": [{"handoff_type": "perm.kickoff", "route": "team_lead"}]},
        )
        db.add_all([src, tgt])
        await db.flush()
        ht = await create_handoff_type(
            db, tenant_id=tenant, name="perm.kickoff", payload_schema={"type": "object"}
        )
        binding = m.ContractBinding(
            tenant_id=tenant,
            event_type="x.done",
            handoff_type_id=ht.id,
            source_department_id=src.id,
            target_department_id=tgt.id,
        )
        db.add(binding)
        role = m.Role(tenant_id=tenant, name="Support runner", kind="human", builtin=False)
        db.add(role)
        await db.flush()
        db.add_all(
            [
                m.RolePermission(tenant_id=tenant, role_id=role.id, permission=p)
                for p in (RUN_START, RUN_CONTROL, perm(FLOW, VIEW))
            ]
        )
        db.add(
            m.OrgMember(
                tenant_id=tenant,
                subject="runner",
                subject_uuid=subject_uuid_for("runner"),
                role_id=role.id,
            )
        )
        await db.flush()
        ids = {
            "tenant": str(tenant),
            "src": str(src.id),
            "tgt": str(tgt.id),
            "ht": str(ht.id),
            "binding": str(binding.id),
        }
    return ids


async def _flow_and_run(client: AsyncClient, ids: dict[str, str]) -> tuple[str, str]:
    admin = _headers(uuid.UUID(ids["tenant"]), "admin", "org_admin")
    pub = await client.post(
        "/api/v1/flows",
        json={
            "spec": {
                "id": "order-to-invoice",
                "version": "1.0.0",
                "trigger": {"event": "x.done", "from_department_id": ids["src"]},
                "stages": [
                    {
                        "id": "invoice",
                        "handoff": {"type": "perm.kickoff", "to_department_id": ids["tgt"]},
                    }
                ],
            }
        },
        headers=admin,
    )
    assert pub.status_code == 201
    run = await client.post(
        f"/api/v1/flow-versions/{pub.json()['id']}/start", json={}, headers=admin
    )
    assert run.status_code == 201
    return str(pub.json()["flowId"]), str(run.json()["id"])


@pytest.mark.parametrize(
    ("subject", "role"),
    [("operator", "operator"), ("runner", "member")],
    ids=["operator", "composed-run-control-role"],
)
async def test_without_flow_manage_a_flow_run_cannot_be_cancelled(
    app_session: AppSessionFactory, client: AsyncClient, subject: str, role: str
) -> None:
    ids = await _setup(app_session)
    _, run_id = await _flow_and_run(client, ids)
    h = _headers(uuid.UUID(ids["tenant"]), subject, role)

    r = await client.post(f"/api/v1/flow-runs/{run_id}/cancel", headers=h)
    assert r.status_code == 403

    admin = _headers(uuid.UUID(ids["tenant"]), "admin", "org_admin")
    still = await client.get(f"/api/v1/flow-runs/{run_id}", headers=admin)
    assert still.json()["status"] == "running"


@pytest.mark.parametrize(
    ("subject", "role"),
    [("operator", "operator"), ("runner", "member")],
    ids=["operator", "composed-run-control-role"],
)
async def test_without_manage_nothing_can_be_archived_or_deleted(
    app_session: AppSessionFactory, client: AsyncClient, subject: str, role: str
) -> None:
    ids = await _setup(app_session)
    flow_id, _ = await _flow_and_run(client, ids)
    h = _headers(uuid.UUID(ids["tenant"]), subject, role)

    archive = await client.post(f"/api/v1/flows/{flow_id}/archive", headers=h)
    assert archive.status_code == 403
    del_type = await client.delete(f"/api/v1/handoff-types/{ids['ht']}", headers=h)
    assert del_type.status_code == 403
    del_binding = await client.delete(f"/api/v1/contract-bindings/{ids['binding']}", headers=h)
    assert del_binding.status_code == 403


async def test_a_department_manager_may_cancel_a_flow_run(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    ids = await _setup(app_session)
    _, run_id = await _flow_and_run(client, ids)
    h = _headers(uuid.UUID(ids["tenant"]), "lead", "dept_manager")

    r = await client.post(f"/api/v1/flow-runs/{run_id}/cancel", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
