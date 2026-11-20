"""GET/POST/DELETE /model-prices -- global, versioned, admin-gated (same
perm(MODEL, ...) gate as /models). Every write is a new version row; there is
no in-place UPDATE and no real DELETE."""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.main import create_app

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


async def test_create_then_list_current_prices() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/model-prices",
                json={
                    "provider": "anthropic",
                    "modelPattern": "testxyz",
                    "priceInUsdPer1M": 1.0,
                    "priceOutUsdPer1M": 2.0,
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            r = await client.get("/api/v1/model-prices", headers=headers)
            assert r.status_code == 200, r.text
            row = next(p for p in r.json() if p["modelPattern"] == "testxyz")
            assert row["priceInUsdPer1M"] == 1.0
            assert row["active"] is True


async def test_a_new_version_supersedes_the_old_one_in_the_current_list() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            await client.post(
                "/api/v1/model-prices",
                json={
                    "provider": "anthropic",
                    "modelPattern": "versiontest",
                    "priceInUsdPer1M": 1.0,
                    "priceOutUsdPer1M": 2.0,
                },
                headers=headers,
            )
            await client.post(
                "/api/v1/model-prices",
                json={
                    "provider": "anthropic",
                    "modelPattern": "versiontest",
                    "priceInUsdPer1M": 5.0,
                    "priceOutUsdPer1M": 6.0,
                },
                headers=headers,
            )
            r = await client.get("/api/v1/model-prices", headers=headers)
            matching = [p for p in r.json() if p["modelPattern"] == "versiontest"]
            assert len(matching) == 1
            assert matching[0]["priceInUsdPer1M"] == 5.0
            # History still has both.
            r = await client.get(
                "/api/v1/model-prices/history",
                params={"provider": "anthropic", "modelPattern": "versiontest"},
                headers=headers,
            )
            assert len(r.json()) == 2


async def test_delete_deactivates_not_deletes() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/model-prices",
                json={
                    "provider": "anthropic",
                    "modelPattern": "deactivateme",
                    "priceInUsdPer1M": 1.0,
                    "priceOutUsdPer1M": 2.0,
                },
                headers=headers,
            )
            price_id = r.json()["id"]
            r = await client.delete(f"/api/v1/model-prices/{price_id}", headers=headers)
            assert r.status_code == 204, r.text
            r = await client.get("/api/v1/model-prices", headers=headers)
            assert not any(p["modelPattern"] == "deactivateme" for p in r.json())
            r = await client.get(
                "/api/v1/model-prices/history",
                params={"provider": "anthropic", "modelPattern": "deactivateme"},
                headers=headers,
            )
            assert len(r.json()) == 2  # original (active) + the deactivation row


async def test_write_forbidden_for_non_admin() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant, role='member')}"}
            r = await client.post(
                "/api/v1/model-prices",
                json={
                    "provider": "anthropic",
                    "modelPattern": "x",
                    "priceInUsdPer1M": 1.0,
                    "priceOutUsdPer1M": 2.0,
                },
                headers=headers,
            )
            assert r.status_code == 403, r.text


async def test_read_only_prices_refuse_tenant_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    # On a shared deployment the price list belongs to every tenant at once,
    # so the operator turns tenant writes off and keeps the reads.
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "model_prices_read_only", True, raising=False)
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            policy = await client.get("/api/v1/model-prices/policy", headers=headers)
            created = await client.post(
                "/api/v1/model-prices",
                json={
                    "provider": "anthropic",
                    "modelPattern": "readonlytest",
                    "priceInUsdPer1M": 1.0,
                    "priceOutUsdPer1M": 2.0,
                },
                headers=headers,
            )
            listed = await client.get("/api/v1/model-prices", headers=headers)
            removed = await client.delete(f"/api/v1/model-prices/{uuid.uuid4()}", headers=headers)

    assert policy.status_code == 200 and policy.json() == {"readOnly": True}
    assert created.status_code == 403
    assert listed.status_code == 200
    assert all(p["modelPattern"] != "readonlytest" for p in listed.json())
    assert removed.status_code == 403


async def test_prices_are_writable_by_default() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            policy = await client.get(
                "/api/v1/model-prices/policy",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert policy.json() == {"readOnly": False}
