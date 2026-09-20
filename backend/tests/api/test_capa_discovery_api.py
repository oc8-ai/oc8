from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.capas.discovery import MANIFEST_FILENAME, invalidate_discovery_cache
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.config import get_settings
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_BUNDLE = """
[plugin]
name = "acme_bundle"
version = "1.0.0"
type = "department_template"
trust = "first_party"
summary = "An example bundle"

[plugin.department_template]
frame = {}

[[plugin.department_template.agents]]
name = "Head of Sales"
is_team_lead = true
"""


def _point_at(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the settings at a temp plugins root. `plugins_path` is read through
    the lru_cached Settings, so set the env var and drop the cache; the caller
    clears it again on teardown so the next test rebuilds from its own env."""
    monkeypatch.setenv("OC8_CAPAS_PATH", str(root))
    get_settings.cache_clear()


@pytest.fixture
def plugins_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    d = tmp_path / "acme_bundle"
    d.mkdir(parents=True)
    (d / MANIFEST_FILENAME).write_text(_BUNDLE)
    _point_at(tmp_path, monkeypatch)
    yield tmp_path
    get_settings.cache_clear()


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    tok = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {tok}"}


async def test_available_lists_the_discovered_plugin_as_not_installed(
    plugins_root: Path,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/capas/available", headers=_h(tenant))
            assert r.status_code == 200, r.text
            row = next(x for x in r.json()["items"] if x["pluginId"] == "acme_bundle")
            assert row["valid"] is True
            assert row["installed"] is False
            assert row["installedVersion"] is None
            assert row["summary"] == "An example bundle"
            assert row["type"] == "department_template"


async def test_install_from_disk_then_shows_installed(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            i = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "acme_bundle"},
                headers=_h(tenant),
            )
            assert i.status_code == 201, i.text
            assert i.json()["name"] == "acme_bundle"
            assert i.json()["semver"] == "1.0.0"

            r = await c.get("/api/v1/capas/available", headers=_h(tenant))
            row = next(x for x in r.json()["items"] if x["pluginId"] == "acme_bundle")
            assert row["installed"] is True
            assert row["installedVersion"] == "1.0.0"


async def test_updating_an_enabled_plugin_is_disabled_pending_consent_but_stays_listed(
    plugins_root: Path,
) -> None:
    """`install_from_disk`'s own two-step consent flow disables the OLD
    version pending re-enable of the new one -- a normal, temporary state
    right after clicking Update, distinguishable from a deliberate operator
    disable or quarantine ONLY via `disabledReason`. The frontend must read
    that reason and NOT treat it as archived (live user report, 2026-08-24:
    updating a Capa made it disappear from the list entirely instead of
    showing the "Enable" prompt it actually needed)."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            installed = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "acme_bundle"},
                headers=_h(tenant),
            )
            assert installed.status_code == 201, installed.text
            capa_id = installed.json()["pluginId"]

            enabled = await c.post(
                f"/api/v1/capas/{capa_id}/enable",
                json={"grantedPermissions": []},
                headers=_h(tenant),
            )
            assert enabled.status_code == 200, enabled.text

            # Bump the on-disk manifest -- the Update button's real trigger.
            (plugins_root / "acme_bundle" / MANIFEST_FILENAME).write_text(
                _BUNDLE.replace('version = "1.0.0"', 'version = "1.0.1"')
            )
            invalidate_discovery_cache()

            updated = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "acme_bundle"},
                headers=_h(tenant),
            )
            assert updated.status_code == 201, updated.text
            assert updated.json()["semver"] == "1.0.1"

            available = await c.get("/api/v1/capas/available", headers=_h(tenant))
            row = next(x for x in available.json()["items"] if x["pluginId"] == "acme_bundle")
            assert row["installationStatus"] == "disabled"
            assert row["disabledReason"] == "plugin update pending consent"


async def test_installed_state_is_per_tenant(plugins_root: Path) -> None:
    """THE critical test: discovery is installation-wide, installation is per
    tenant. Tenant A installing must not make it look installed for tenant B."""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "acme_bundle"},
                headers=_h(tenant_a),
            )
            assert r.status_code == 201, r.text

            b = await c.get("/api/v1/capas/available", headers=_h(tenant_b))
            row = next(x for x in b.json()["items"] if x["pluginId"] == "acme_bundle")
            assert row["installed"] is False
            assert row["installedVersion"] is None


async def test_install_unknown_plugin_404(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "nope"},
                headers=_h(tenant),
            )
            assert r.status_code == 404


async def test_install_twice_is_rejected(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            body = {"pluginId": "acme_bundle"}
            assert (
                await c.post(
                    "/api/v1/capas/install-from-disk", json=body, headers=_h(tenant)
                )
            ).status_code == 201
            again = await c.post(
                "/api/v1/capas/install-from-disk", json=body, headers=_h(tenant)
            )
            assert again.status_code == 400
            assert "already installed" in again.text


async def test_invalid_manifest_is_listed_but_not_installable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "broken"
    d.mkdir(parents=True)
    (d / MANIFEST_FILENAME).write_text('[plugin]\nname = "broken"\n')  # no version
    _point_at(tmp_path, monkeypatch)
    tenant = uuid.uuid4()
    app = create_app()
    try:
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                lst = await c.get("/api/v1/capas/available", headers=_h(tenant))
                row = next(x for x in lst.json()["items"] if x["pluginId"] == "broken")
                assert row["valid"] is False
                assert row["error"]

                r = await c.post(
                    "/api/v1/capas/install-from-disk",
                    json={"pluginId": "broken"},
                    headers=_h(tenant),
                )
                assert r.status_code == 400
    finally:
        get_settings.cache_clear()


async def test_available_lists_a_custom_capa_with_no_disk_folder(
    app_session: AppSessionFactory,
) -> None:
    """A capa installed via the custom-MCP wizard (`origin="custom"`) has no
    `plugin.toml` on disk at all -- `discover_plugins()` never sees it, so
    without merging DB-only rows in, an installed-and-enabled custom capa
    would silently never appear here (live bug found 2026-09-20 walking
    through the wizard end to end: install/enable succeeded, but the Capas
    page's Installed tab stayed empty)."""
    tenant = uuid.uuid4()
    manifest = {
        "name": "acme_billing",
        "version": "1.0.0",
        "type": "tool_pack",
        "label": "Acme Billing",
        "summary": "Acme's billing REST API.",
        "tool_pack": {
            "connections": [
                {
                    "key": "default",
                    "name": "acme_billing",
                    "server_url": "https://api.acme.example/v1",
                    "transport": "manual_http",
                    "config": {"auth_header_name": "Authorization"},
                }
            ]
        },
    }
    async with app_session(tenant) as db:
        version = await install_plugin(
            db, tenant_id=tenant, manifest_data=manifest, origin="custom"
        )
        await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
        await db.commit()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/capas/available", headers=_h(tenant))
            assert r.status_code == 200, r.text
            row = next(x for x in r.json()["items"] if x["pluginId"] == "acme_billing")
            assert row["installed"] is True
            assert row["installedVersion"] == "1.0.0"
            assert row["installationStatus"] == "enabled"
            assert row["label"] == "Acme Billing"
            assert row["type"] == "tool_pack"
            assert row["databaseId"] == str(version.capa_id)


async def test_install_requires_admin(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "acme_bundle"},
                headers=_h(tenant, "member"),
            )
            assert r.status_code == 403
