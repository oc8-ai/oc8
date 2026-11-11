"""A capa provider declares which of its models think, and its adapter
receives the level (spec 2026-11-11 §6, §10)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.capas import contributions, loader
from oc8.capas.discovery import find_plugin
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.config import get_settings
from oc8.main import create_app
from oc8.modelrouter.fallback import complete_with_fallback
from oc8.modelrouter.router import ModelRouter
from oc8.modelrouter.sampling import resolve_params
from oc8.modelrouter.types import NeutralMessage
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(FIXTURES))
    get_settings.cache_clear()
    loader.reset_for_tests()
    contributions.reset_for_tests()
    yield
    loader.reset_for_tests()
    contributions.reset_for_tests()
    get_settings.cache_clear()


async def _enable_demo(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    found = find_plugin("demo_provider")
    assert found is not None and found.manifest is not None
    async with app_session(tenant) as db:
        version = await install_plugin(db, tenant_id=tenant, manifest_data=found.manifest)
        await enable_plugin(
            db,
            tenant_id=tenant,
            capa_id=version.capa_id,
            granted_permissions=list(version.permissions),
        )


def _h(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_the_capa_declaration_prefills_a_new_model(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _enable_demo(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            think = await c.post(
                "/api/v1/models",
                json={"provider": "demo-llm", "model": "demo-think-1", "locality": "cloud"},
                headers=_h(tenant),
            )
            plain = await c.post(
                "/api/v1/models",
                json={"provider": "demo-llm", "model": "demo-1", "locality": "cloud"},
                headers=_h(tenant),
            )
    assert think.status_code == 201, think.text
    assert think.json()["supportsReasoning"] is True
    assert plain.json()["supportsReasoning"] is False


async def test_a_capa_whose_declaration_raises_still_creates_the_model(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _enable_demo(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/models",
                json={"provider": "demo-llm", "model": "demo-crash", "locality": "cloud"},
                headers=_h(tenant),
            )
    assert r.status_code == 201, r.text
    assert r.json()["supportsReasoning"] is False


async def test_the_level_reaches_the_capa_adapter(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _enable_demo(app_session, tenant)
    async with app_session(tenant) as db:
        cfg = m.ModelConfig(
            tenant_id=tenant, provider="demo-llm", model="demo-think-1", supports_reasoning=True
        )
        db.add(cfg)
        await db.flush()
        result = await complete_with_fallback(
            db,
            ModelRouter(get_settings()),
            tenant_id=tenant,
            agent_id=None,
            primary=cfg,
            no_config_provider="demo-llm",
            no_config_model="demo-think-1",
            messages=[NeutralMessage(role="user", content="hi")],
            tools=[],
            params=resolve_params(cfg, definition={"model_params": {"reasoning": "thorough"}}),
            request_id=uuid.uuid4(),
            contains_restricted=False,
        )
    assert result.text == "hello from a plugin provider (reasoning: thorough)"
