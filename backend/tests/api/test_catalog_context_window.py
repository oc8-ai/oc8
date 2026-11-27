"""A model's recorded context window has a floor (chat harness spec §5.9).

The chat checkpoints at 40 % / 70 % of the window and replays the turns that
fit. A window of a few hundred tokens cannot hold the system prefix and one
turn, so every chat on that model would checkpoint on every turn. 0 or omitted
still means "not recorded".
"""

from __future__ import annotations

import uuid

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app

_MODEL = {"provider": "ollama", "model": "llama3.1:8b", "locality": "local"}


def _headers() -> dict[str, str]:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    token = get_identity_provider().mint(tenant_id=tenant, subject="u", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_a_window_below_the_floor_is_refused_with_the_reason() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = _headers()
            r = await client.post("/api/v1/models", json=_MODEL, headers=headers)
            assert r.status_code == 201, r.text
            model_id = r.json()["id"]

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={**_MODEL, "contextWindow": 4095},
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert "4096" in r.json()["detail"]

            r = await client.post(
                "/api/v1/models", json={**_MODEL, "contextWindow": 1000}, headers=headers
            )
            assert r.status_code == 422, r.text

            r = await client.get("/api/v1/models", headers=headers)
            assert r.status_code == 200, r.text
            [row] = [x for x in r.json() if x["id"] == model_id]
            assert row["contextWindow"] is None


async def test_the_floor_itself_and_not_recorded_are_accepted() -> None:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = _headers()
            r = await client.post("/api/v1/models", json=_MODEL, headers=headers)
            assert r.status_code == 201, r.text
            model_id = r.json()["id"]

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={**_MODEL, "contextWindow": 4096},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["contextWindow"] == 4096

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={**_MODEL, "contextWindow": 0},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["contextWindow"] is None
