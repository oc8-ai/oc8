# backend/tests/api/test_plugin_depends_install.py
"""`install-from-disk` recursively installs `plugin_depends` (design §9) but
never enables what it installs on the requester's behalf -- oc8's existing
install/enable consent-gate separation still applies to a dependency exactly
as it would to a plugin an operator installed by hand.

None of the 21 shipped plugins declare `plugin_depends` yet (spec §11 open
question), so this file builds small, throwaway fixture plugins on disk,
mirroring `test_plugins_endpoint.py::test_setup_validate_runs_and_a_rejection_leaves_nothing_behind`
(a real plugin.toml written under `tmp_path`, `OC8_CAPAS_PATH` monkeypatched
to it, `get_settings.cache_clear()` to pick it up) rather than
`cli_harness/test_registration.py`'s convention of pointing at the real
`plugins/` directory, since these plugins don't exist there and shouldn't --
they're not being shipped.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID) -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role="org_admin")


def _write_plugin(folder: Path, name: str, depends: list[str]) -> None:
    plugin_dir = folder / name
    plugin_dir.mkdir()
    depends_toml = repr(depends).replace("'", '"')
    (plugin_dir / "plugin.toml").write_text(
        f"""
[plugin]
name = "{name}"
version = "1.0.0"
type = "connector"
trust = "first_party"
summary = "x"
plugin_depends = {depends_toml}
""",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _plugins_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_install_with_plugin_depends_also_installs_but_never_enables_the_dependency(
    tmp_path: Path, app_session: AppSessionFactory
) -> None:
    dep_name = f"acme_dep_{uuid.uuid4().hex[:8]}"
    main_name = f"acme_main_{uuid.uuid4().hex[:8]}"
    _write_plugin(tmp_path, dep_name, [])
    _write_plugin(tmp_path, main_name, [dep_name])

    tenant = uuid.uuid4()
    app = create_app()
    headers = {"Authorization": f"Bearer {_token(tenant)}"}
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": main_name, "enable": False},
                headers=headers,
            )
            assert resp.status_code == 201, resp.text
            assert resp.json()["name"] == main_name

    async with app_session(tenant) as db:
        main_plugin = (
            await db.execute(
                select(m.Capa).where(m.Capa.tenant_id == tenant, m.Capa.name == main_name)
            )
        ).scalar_one()
        dep_plugin = (
            await db.execute(
                select(m.Capa).where(m.Capa.tenant_id == tenant, m.Capa.name == dep_name)
            )
        ).scalar_one_or_none()
        assert dep_plugin is not None, "the dependency was never auto-installed"
        assert main_plugin.current_version_id is not None
        assert dep_plugin.current_version_id is not None

        # Never auto-enabled: no CapaInstallation row exists for either the
        # requested plugin or its dependency -- installing never enables
        # anything, on its own behalf or a dependency's, and this test never
        # called /enable for either.
        installations = (
            (
                await db.execute(
                    select(m.CapaInstallation).where(m.CapaInstallation.tenant_id == tenant)
                )
            )
            .scalars()
            .all()
        )
        assert installations == []


async def test_installing_a_plugin_whose_dependency_is_already_installed_does_not_reinstall_it(
    tmp_path: Path, app_session: AppSessionFactory
) -> None:
    dep_name = f"acme_dep_{uuid.uuid4().hex[:8]}"
    main_name = f"acme_main_{uuid.uuid4().hex[:8]}"
    _write_plugin(tmp_path, dep_name, [])
    _write_plugin(tmp_path, main_name, [dep_name])

    tenant = uuid.uuid4()
    app = create_app()
    headers = {"Authorization": f"Bearer {_token(tenant)}"}
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            first = await client.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": dep_name},
                headers=headers,
            )
            assert first.status_code == 201, first.text

            # The dependency is already installed for this tenant. Installing
            # the main plugin must not try to install it again -- that would
            # hit DuplicateVersionError and 400 the whole request.
            second = await client.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": main_name},
                headers=headers,
            )
            assert second.status_code == 201, second.text

    async with app_session(tenant) as db:
        dep_versions = (
            (
                await db.execute(
                    select(m.CapaVersion)
                    .join(m.Capa, m.Capa.id == m.CapaVersion.capa_id)
                    .where(m.Capa.tenant_id == tenant, m.Capa.name == dep_name)
                )
            )
            .scalars()
            .all()
        )
        assert len(dep_versions) == 1


async def test_installing_a_plugin_whose_dependency_is_missing_from_disk_returns_404(
    tmp_path: Path,
) -> None:
    main_name = f"acme_main_{uuid.uuid4().hex[:8]}"
    _write_plugin(tmp_path, main_name, ["nonexistent_dep"])

    tenant = uuid.uuid4()
    app = create_app()
    headers = {"Authorization": f"Bearer {_token(tenant)}"}
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": main_name},
                headers=headers,
            )
            assert resp.status_code == 404, resp.text
            assert "nonexistent_dep" in resp.text
