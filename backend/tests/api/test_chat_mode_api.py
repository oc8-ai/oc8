"""PUT /chat/sessions/{id}/mode: the member keeps a sticky mode on their own
chat, or leaves it (✕). Owner-only, and only sticky modes."""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_the_owner_sets_and_leaves_agent_mode() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            me = _headers(tenant, "op")
            agent_id = (await c.get("/api/v1/assistant", headers=me)).json()["agentId"]
            created = await c.post("/api/v1/chat/sessions", json={"agentId": agent_id}, headers=me)
            sid = created.json()["id"]
            assert created.json()["mode"] is None

            url = f"/api/v1/chat/sessions/{sid}/mode"
            on = await c.put(url, json={"mode": "agent"}, headers=me)
            assert on.status_code == 200, on.text
            assert on.json()["mode"] == "agent"
            detail = await c.get(f"/api/v1/chat/sessions/{sid}", headers=me)
            assert detail.json()["mode"] == "agent"

            plan = await c.put(url, json={"mode": "plan"}, headers=me)
            assert plan.status_code == 422

            unknown = await c.put(url, json={"mode": "nonsense"}, headers=me)
            assert unknown.status_code == 422

            other = await c.put(url, json={"mode": None}, headers=_headers(tenant, "eve"))
            assert other.status_code == 404

            off = await c.put(url, json={"mode": None}, headers=me)
            assert off.status_code == 200 and off.json()["mode"] is None


async def test_the_picker_learns_which_modes_stick() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            modes = (await c.get("/api/v1/chat/modes", headers=_headers(tenant, "op"))).json()
    by_key = {mode["key"]: mode for mode in modes}
    assert by_key["agent"]["sticky"] is True
    assert by_key["plan"]["sticky"] is False


async def test_agent_mode_is_the_copilot_chats_alone(app_session: AppSessionFactory) -> None:
    """Another agent's chat refuses Agent mode, sticky or typed (422, the
    sentence the composer shows)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb")
        db.add(dept)
        await db.flush()
        nora = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", status="running")
        db.add(nora)
        await db.flush()
        nora_id = nora.id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            me = _headers(tenant, "op")
            created = await c.post(
                "/api/v1/chat/sessions", json={"agentId": str(nora_id)}, headers=me
            )
            assert created.status_code == 201, created.text
            sid = created.json()["id"]

            on = await c.put(
                f"/api/v1/chat/sessions/{sid}/mode", json={"mode": "agent"}, headers=me
            )
            assert on.status_code == 422
            assert on.json()["detail"] == "agent mode is only available in the Copilot chat"

            typed = await c.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"message": "/agent build me an invoice checker"},
                headers=me,
            )
            assert typed.status_code == 422
            assert typed.json()["detail"] == "agent mode is only available in the Copilot chat"
            messages = await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=me)
            assert messages.json() == [], "nothing was said"
            detail = await c.get(f"/api/v1/chat/sessions/{sid}", headers=me)
            assert detail.json()["mode"] is None
