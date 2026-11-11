"""A new model config's "Can think" starts from the provider's declaration;
the admin can change it (spec 2026-11-11 §6, §8)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _create(c: AsyncClient, tenant: uuid.UUID, **body: Any) -> dict[str, Any]:
    r = await c.post("/api/v1/models", json={"locality": "cloud", **body}, headers=_h(tenant))
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


async def test_create_prefills_from_the_declaration_and_update_changes_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            claude = await _create(c, tenant, provider="anthropic", model="claude-sonnet-5")
            assert claude["supportsReasoning"] is True
            llama = await _create(c, tenant, provider="ollama", model="llama3.1:8b")
            assert llama["supportsReasoning"] is False
            forced = await _create(
                c, tenant, provider="ollama", model="llama3.1:8b", supportsReasoning=True
            )
            assert forced["supportsReasoning"] is True

            off = await c.patch(
                f"/api/v1/models/{claude['id']}",
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "supportsReasoning": False,
                },
                headers=_h(tenant),
            )
            assert off.status_code == 200 and off.json()["supportsReasoning"] is False
            untouched = await c.patch(
                f"/api/v1/models/{claude['id']}",
                json={"provider": "anthropic", "model": "claude-sonnet-5"},
                headers=_h(tenant),
            )
            assert untouched.json()["supportsReasoning"] is False


async def test_the_form_can_ask_for_the_suggestion(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yes = await c.get(
                "/api/v1/models/reasoning-support",
                params={"provider": "ollama", "model": "qwen3:8b"},
                headers=_h(tenant),
            )
            assert yes.status_code == 200 and yes.json() == {"supportsReasoning": True}
            unknown = await c.get(
                "/api/v1/models/reasoning-support",
                params={"provider": "nope", "model": "x"},
                headers=_h(tenant),
            )
            assert unknown.json() == {"supportsReasoning": False}


async def test_the_suggestion_needs_model_manage(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                "/api/v1/models/reasoning-support",
                params={"provider": "ollama", "model": "qwen3:8b"},
                headers=_h(tenant, role="member"),
            )
    assert r.status_code == 403, r.text
