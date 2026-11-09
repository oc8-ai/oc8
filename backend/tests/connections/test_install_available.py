"""Installing a plugin makes it available in the same step (connections
design), but never grants a permission an update newly asks for."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from oc8 import config
from oc8 import models as m
from oc8.capas.discovery import invalidate_discovery_cache
from tests.conftest import AppSessionFactory
from tests.connections.conftest import _PLUGINS_DIR
from tests.connections.helpers import api, headers

pytestmark = pytest.mark.asyncio


async def _status(app_session: AppSessionFactory, tenant: uuid.UUID, name: str) -> str:
    async with app_session(tenant) as db:
        capa = (await db.execute(select(m.Capa).where(m.Capa.name == name))).scalar_one()
        inst = (
            await db.execute(
                select(m.CapaInstallation).where(m.CapaInstallation.capa_id == capa.id)
            )
        ).scalar_one_or_none()
        # No installation row yet is how a never-enabled install looks
        # (lifecycle._installation creates it as "installed" on first touch).
        return inst.status if inst is not None else "installed"


async def test_installing_makes_it_available(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        r = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=admin
        )
        assert r.status_code in (200, 201), r.text
        listed = (await c.get("/api/v1/connections/integrations", headers=admin)).json()
    assert await _status(app_session, tenant, "odoo_mcp") == "enabled"
    odoo = next(row for row in listed if row["name"] == "odoo_mcp")
    assert odoo["available"] is True
    assert odoo["permissions"]


async def test_the_old_two_step_install_still_exists(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        await c.post(
            "/api/v1/capas/install-from-disk",
            json={"pluginId": "odoo_mcp", "enable": False},
            headers=admin,
        )
        listed = (await c.get("/api/v1/connections/integrations", headers=admin)).json()
    assert await _status(app_session, tenant, "odoo_mcp") == "installed"
    odoo = next(row for row in listed if row["name"] == "odoo_mcp")
    assert odoo["available"] is False
    assert sorted(odoo["permissions"]) == ["mcp:connect", "secrets:use"]


async def test_installing_still_needs_the_plugin_manage_permission(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    member = headers(tenant, "ben@acme.test", "member")
    async with api() as c:
        r = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=member
        )
    assert r.status_code == 403, r.text


@pytest.fixture
def odoo_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable copy of the Odoo plugin, so a test can bump its manifest."""
    shutil.copytree(_PLUGINS_DIR / "odoo_mcp", tmp_path / "odoo_mcp")
    monkeypatch.setenv("OC8_CAPAS_PATH", str(tmp_path))
    config.get_settings.cache_clear()
    invalidate_discovery_cache()
    return tmp_path / "odoo_mcp" / "plugin.toml"


def _bump(manifest: Path, *, extra_permission: str | None = None) -> None:
    text = manifest.read_text()
    text = text.replace('version = "1.8.6"', 'version = "9.9.9"', 1)
    if extra_permission is not None:
        text = text.replace(
            'permissions = ["mcp:connect", "secrets:use"]',
            f'permissions = ["mcp:connect", "secrets:use", "{extra_permission}"]',
            1,
        )
    assert "9.9.9" in text and (extra_permission is None or extra_permission in text)
    manifest.write_text(text)
    invalidate_discovery_cache()


async def test_an_update_asking_nothing_new_stays_available(
    app_session: AppSessionFactory, odoo_copy: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        first = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=admin
        )
        assert first.status_code == 201, first.text
        _bump(odoo_copy)
        updated = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=admin
        )
        assert updated.status_code == 201, updated.text
        assert updated.json()["semver"] == "9.9.9"
    assert await _status(app_session, tenant, "odoo_mcp") == "enabled"


async def test_an_update_asking_a_new_permission_waits_for_consent(
    app_session: AppSessionFactory, odoo_copy: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        first = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=admin
        )
        assert first.status_code == 201, first.text
        _bump(odoo_copy, extra_permission="network:egress")
        updated = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=admin
        )
        assert updated.status_code == 201, updated.text
        listed = (await c.get("/api/v1/connections/integrations", headers=admin)).json()
    assert await _status(app_session, tenant, "odoo_mcp") == "disabled"
    async with app_session(tenant) as db:
        inst = (await db.execute(select(m.CapaInstallation))).scalar_one()
        assert "network:egress" not in inst.granted_permissions
    # Pending consent is not an integration anyone can connect right now.
    assert all(row["name"] != "odoo_mcp" or row["available"] is False for row in listed)


async def test_a_disabled_plugin_is_not_re_enabled_by_reinstalling(
    app_session: AppSessionFactory, odoo_copy: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        first = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=admin
        )
        capa_id = first.json()["pluginId"]
        off = await c.post(f"/api/v1/capas/{capa_id}/disable", json={}, headers=admin)
        assert off.status_code == 200, off.text
        _bump(odoo_copy)
        updated = await c.post(
            "/api/v1/capas/install-from-disk", json={"pluginId": "odoo_mcp"}, headers=admin
        )
        assert updated.status_code == 201, updated.text
    assert await _status(app_session, tenant, "odoo_mcp") == "disabled"


async def test_setting_up_a_never_enabled_install_enables_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        r = await c.post(
            "/api/v1/capas/install-from-disk",
            json={"pluginId": "engineering_dev_agent", "enable": False},
            headers=admin,
        )
        assert r.status_code == 201, r.text
        capa_id = r.json()["pluginId"]
        assert await _status(app_session, tenant, "engineering_dev_agent") == "installed"
        setup = await c.post(
            f"/api/v1/capas/{capa_id}/setup",
            json={"values": {"repo": "acme/widgets", "label": "agent-ready", "base_branch": ""}},
            headers=admin,
        )
    assert setup.status_code == 200, setup.text
    assert await _status(app_session, tenant, "engineering_dev_agent") == "enabled"
