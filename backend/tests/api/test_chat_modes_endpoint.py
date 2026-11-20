"""`GET /chat/modes` -- one list, so the picker and the enforcement agree."""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.chat.modes import MODES
from oc8.main import create_app

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_it_serves_every_mode_in_picker_order() -> None:
    """A caller holding copilot:use gets exactly `MODES`, in picker order,
    each with a non-empty summary -- and `/budget` is deliberately absent,
    since it is not a `send_message` mode."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/chat/modes", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert [entry["key"] for entry in body] == list(MODES)
            assert all(entry["summary"] for entry in body)
            assert "budget" not in {entry["key"] for entry in body}
