"""The whole loop over the real HTTP surface: a plugin folder with Python code
is discovered, installed, enabled, and its connector then works -- for that
tenant and no other."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.capas import contributions, loader
from oc8.config import get_settings
from oc8.main import create_app

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _plugins_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(FIXTURES))
    get_settings.cache_clear()
    loader.reset_for_tests()
    contributions.reset_for_tests()
    yield
    loader.reset_for_tests()
    contributions.reset_for_tests()
    get_settings.cache_clear()


def _h(tenant: uuid.UUID) -> dict[str, str]:
    tok = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {tok}"}


async def test_a_code_plugin_goes_from_folder_to_working_connector() -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # 1. It is discovered on disk, not yet installed.
            avail = await c.get("/api/v1/capas/available", headers=_h(tenant_a))
            row = next(x for x in avail.json()["items"] if x["pluginId"] == "demo_connector")
            assert row["valid"] is True and row["installed"] is False
            assert row["type"] == "connector"

            # 2. Install it for tenant A.
            inst = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "demo_connector", "enable": False},
                headers=_h(tenant_a),
            )
            assert inst.status_code == 201, inst.text
            plugin_id = inst.json()["pluginId"]

            # 3. Installed but not enabled: its connector must NOT work yet.
            kb = uuid.uuid4()
            body = {
                "connectorType": "demo",
                "name": "demo source",
                "config": {},
                "kbId": str(kb),
            }
            early = await c.post("/api/v1/knowledge/sources", json=body, headers=_h(tenant_a))
            assert early.status_code == 400, early.text

            # 4. Enable it -- this is where consent happens.
            en = await c.post(
                f"/api/v1/capas/{plugin_id}/enable",
                json={"grantedPermissions": []},
                headers=_h(tenant_a),
            )
            assert en.status_code in (200, 201), en.text

            # 5. Now the plugin's connector is usable by tenant A.
            ok = await c.post("/api/v1/knowledge/sources", json=body, headers=_h(tenant_a))
            assert ok.status_code == 201, ok.text
            assert ok.json()["connectorType"] == "demo"

            # 6. Tenant B never installed it. The module IS loaded in this
            #    process now -- that must not help tenant B at all.
            denied = await c.post(
                "/api/v1/knowledge/sources",
                json={**body, "kbId": str(uuid.uuid4())},
                headers=_h(tenant_b),
            )
            assert denied.status_code == 400, denied.text
            assert "demo" in denied.text
