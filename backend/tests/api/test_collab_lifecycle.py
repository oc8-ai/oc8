"""Removing contract bindings and handoff types (§14a.2, §14a.3).

Handoff types and contract bindings could be created but never removed: an
unwanted binding kept producing pending handoffs on every emit. These pin the
way out, and that an archived department neither emits nor receives.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.collab.handoff import create_handoff_type
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

TENANT = uuid.UUID(str(ACME_TENANT_ID))


def _headers() -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=TENANT, subject="u", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


async def _emitting_pair(
    app_session: AppSessionFactory, event: str, type_name: str, *, target_archived: bool = False
) -> tuple[str, str, str]:
    async with app_session(TENANT) as db:
        src = m.Department(tenant_id=TENANT, name=f"src-{_suffix()}", frame={"emits": [event]})
        tgt = m.Department(
            tenant_id=TENANT,
            name=f"tgt-{_suffix()}",
            frame={"intakes": [{"handoff_type": type_name, "route": "team_lead"}]},
            deleted_at=dt.datetime.now(tz=dt.UTC) if target_archived else None,
        )
        db.add_all([src, tgt])
        await db.flush()
        ht = await create_handoff_type(
            db, tenant_id=TENANT, name=type_name, payload_schema={"type": "object"}
        )
        return str(src.id), str(tgt.id), str(ht.id)


async def _bind(client: AsyncClient, event: str, ht: str, src: str, tgt: str) -> str:
    r = await client.post(
        "/api/v1/contract-bindings",
        json={
            "eventType": event,
            "handoffTypeId": ht,
            "sourceDepartmentId": src,
            "targetDepartmentId": tgt,
        },
        headers=_headers(),
    )
    assert r.status_code == 201
    return str(r.json()["id"])


# --- contract bindings ------------------------------------------------------


async def test_a_deleted_binding_no_longer_creates_handoffs(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    event, type_name = f"ev.{_suffix()}", f"ht.{_suffix()}"
    src, tgt, ht = await _emitting_pair(app_session, event, type_name)
    binding = await _bind(client, event, ht, src, tgt)
    listed = await client.get("/api/v1/contract-bindings", headers=_headers())
    mine = next(b for b in listed.json() if b["id"] == binding)
    assert mine["handoffTypeId"] == ht

    gone = await client.delete(f"/api/v1/contract-bindings/{binding}", headers=_headers())
    assert gone.status_code == 204

    listed = await client.get("/api/v1/contract-bindings", headers=_headers())
    assert all(b["id"] != binding for b in listed.json())

    emitted = await client.post(
        f"/api/v1/departments/{src}/emit", json={"eventType": event}, headers=_headers()
    )
    assert emitted.status_code == 200
    assert emitted.json()["handoffs"] == []

    again = await client.delete(f"/api/v1/contract-bindings/{binding}", headers=_headers())
    assert again.status_code == 404


async def test_emit_refuses_an_archived_source_department(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    event, type_name = f"ev.{_suffix()}", f"ht.{_suffix()}"
    src, tgt, ht = await _emitting_pair(app_session, event, type_name)
    await _bind(client, event, ht, src, tgt)
    async with app_session(TENANT) as db:
        dept = await db.get(m.Department, uuid.UUID(src))
        assert dept is not None
        dept.deleted_at = dt.datetime.now(tz=dt.UTC)

    r = await client.post(
        f"/api/v1/departments/{src}/emit", json={"eventType": event}, headers=_headers()
    )
    assert r.status_code == 404


async def test_emit_skips_an_archived_target_department(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    event, type_name = f"ev.{_suffix()}", f"ht.{_suffix()}"
    src, tgt, ht = await _emitting_pair(app_session, event, type_name, target_archived=True)
    await _bind(client, event, ht, src, tgt)

    r = await client.post(
        f"/api/v1/departments/{src}/emit", json={"eventType": event}, headers=_headers()
    )
    assert r.status_code == 200
    assert r.json()["handoffs"] == []


# --- handoff types ----------------------------------------------------------


async def test_an_unused_handoff_type_can_be_deleted(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/handoff-types",
        json={"name": f"ht.{_suffix()}", "payloadSchema": {"type": "object"}},
        headers=_headers(),
    )
    type_id = created.json()["id"]

    r = await client.delete(f"/api/v1/handoff-types/{type_id}", headers=_headers())
    assert r.status_code == 204
    listed = await client.get("/api/v1/handoff-types", headers=_headers())
    assert all(t["id"] != type_id for t in listed.json())
    again = await client.delete(f"/api/v1/handoff-types/{type_id}", headers=_headers())
    assert again.status_code == 404


async def test_a_handoff_type_in_use_is_kept_with_a_reason(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    event, type_name = f"ev.{_suffix()}", f"ht.{_suffix()}"
    src, tgt, ht = await _emitting_pair(app_session, event, type_name)
    await _bind(client, event, ht, src, tgt)
    made = await client.post(
        "/api/v1/handoffs",
        json={
            "handoffTypeId": ht,
            "sourceDepartmentId": src,
            "targetDepartmentId": tgt,
            "payload": {},
        },
        headers=_headers(),
    )
    assert made.status_code == 201

    r = await client.delete(f"/api/v1/handoff-types/{ht}", headers=_headers())
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "1 handoff" in detail
    assert "1 contract binding" in detail


async def test_a_handoff_type_a_flow_uses_is_kept(client: AsyncClient) -> None:
    type_name = f"ht.{_suffix()}"
    created = await client.post(
        "/api/v1/handoff-types",
        json={"name": type_name, "payloadSchema": {"type": "object"}},
        headers=_headers(),
    )
    type_id = created.json()["id"]
    pub = await client.post(
        "/api/v1/flows",
        json={"spec": _spec(f"flow-{_suffix()}", type_name, type_name)},
        headers=_headers(),
    )
    assert pub.status_code == 201

    r = await client.delete(f"/api/v1/handoff-types/{type_id}", headers=_headers())
    assert r.status_code == 409
    assert "1 flow" in r.json()["detail"]

    # Archiving the flow releases the type.
    archived = await client.post(
        f"/api/v1/flows/{pub.json()['flowId']}/archive", headers=_headers()
    )
    assert archived.status_code == 200
    r = await client.delete(f"/api/v1/handoff-types/{type_id}", headers=_headers())
    assert r.status_code == 204


# --- flows and flow runs ----------------------------------------------------


def _spec(flow_id: str, first: str, second: str) -> dict[str, object]:
    to_dept = str(uuid.uuid4())
    return {
        "id": flow_id,
        "version": "1.0.0",
        "trigger": {"event": "x.happened", "from_department_id": str(uuid.uuid4())},
        "stages": [
            {"id": "kickoff", "handoff": {"type": first, "to_department_id": to_dept}},
            {
                "id": "build",
                "after": "kickoff.completed",
                "handoff": {"type": second, "to_department_id": to_dept},
            },
        ],
    }
