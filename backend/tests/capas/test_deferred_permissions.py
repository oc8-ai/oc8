"""A capa can defer a permission until a contribution needs it.

An update that only adds a deferred permission keeps an enabled capa enabled;
the deferred permission is granted later, through its own route, by someone
who may manage capas.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.capas.manifest import ManifestError, parse_manifest
from tests.capas.deferred_capa import NAME, reset, use_capas_path, write_capa
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers

BASE = ["mcp:connect", "secrets:use"]


@pytest.fixture
def capas_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    use_capas_path(tmp_path, monkeypatch)
    yield tmp_path
    reset()


async def _installation(app_session: AppSessionFactory, tenant: uuid.UUID) -> m.CapaInstallation:
    async with app_session(tenant) as db:
        capa = (await db.execute(select(m.Capa).where(m.Capa.name == NAME))).scalar_one()
        return (
            await db.execute(
                select(m.CapaInstallation).where(m.CapaInstallation.capa_id == capa.id)
            )
        ).scalar_one()


def test_manifest_rejects_deferred_permission_not_in_permissions() -> None:
    with pytest.raises(ManifestError, match="must also be listed in permissions"):
        parse_manifest(
            {
                "name": "x",
                "version": "1.0.0",
                "permissions": ["a"],
                "deferred_permissions": ["knowledge:write"],
            }
        )
    ok = parse_manifest(
        {
            "name": "x",
            "version": "1.0.0",
            "permissions": ["a", "knowledge:write"],
            "deferred_permissions": ["knowledge:write"],
        }
    )
    assert ok.deferred_permissions == ["knowledge:write"]


async def _install(client, admin) -> str:  # type: ignore[no-untyped-def]
    r = await client.post("/api/v1/capas/install-from-disk", json={"pluginId": NAME}, headers=admin)
    assert r.status_code == 201, r.text
    return str(r.json()["pluginId"])


@pytest.mark.asyncio
async def test_update_adding_only_deferred_permission_keeps_plugin_enabled(
    app_session: AppSessionFactory, capas_root: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    write_capa(capas_root, permissions=BASE)
    async with api() as c:
        await _install(c, admin)
        write_capa(
            capas_root,
            version="1.1.0",
            permissions=[*BASE, "knowledge:write"],
            deferred=["knowledge:write"],
        )
        await _install(c, admin)
    inst = await _installation(app_session, tenant)
    assert inst.status == "enabled"
    assert sorted(inst.granted_permissions) == sorted(BASE)


@pytest.mark.asyncio
async def test_update_adding_non_deferred_permission_still_needs_consent(
    app_session: AppSessionFactory, capas_root: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    write_capa(capas_root, permissions=BASE)
    async with api() as c:
        await _install(c, admin)
        write_capa(capas_root, version="1.1.0", permissions=[*BASE, "knowledge:write"])
        await _install(c, admin)
    inst = await _installation(app_session, tenant)
    assert inst.status != "enabled"
    assert "knowledge:write" not in inst.granted_permissions


@pytest.mark.asyncio
async def test_grant_deferred_permission(app_session: AppSessionFactory, capas_root: Path) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    member = headers(tenant, "ben@acme.test", "member")
    write_capa(capas_root, permissions=[*BASE, "knowledge:write"], deferred=["knowledge:write"])
    async with api() as c:
        capa_id = await _install(c, admin)
        inst = await _installation(app_session, tenant)
        assert inst.status == "enabled"
        assert "knowledge:write" not in inst.granted_permissions

        forbidden = await c.post(
            f"/api/v1/capas/{capa_id}/grant", json={"permission": "knowledge:write"}, headers=member
        )
        assert forbidden.status_code == 403, forbidden.text

        r = await c.post(
            f"/api/v1/capas/{capa_id}/grant", json={"permission": "knowledge:write"}, headers=admin
        )
        assert r.status_code == 200, r.text
        assert "knowledge:write" in r.json()["grantedPermissions"]
        assert r.json()["status"] == "enabled"

        again = await c.post(
            f"/api/v1/capas/{capa_id}/grant", json={"permission": "knowledge:write"}, headers=admin
        )
        assert again.status_code == 200, again.text
        assert again.json()["grantedPermissions"].count("knowledge:write") == 1

        not_deferred = await c.post(
            f"/api/v1/capas/{capa_id}/grant", json={"permission": "mcp:connect"}, headers=admin
        )
        assert not_deferred.status_code == 400, not_deferred.text

        # A later update keeps what was granted.
        write_capa(
            capas_root,
            version="1.1.0",
            permissions=[*BASE, "knowledge:write"],
            deferred=["knowledge:write"],
        )
        await _install(c, admin)
        inst = await _installation(app_session, tenant)
        assert inst.status == "enabled"
        assert "knowledge:write" in inst.granted_permissions

        off = await c.post(f"/api/v1/capas/{capa_id}/disable", json={}, headers=admin)
        assert off.status_code == 200, off.text
        conflict = await c.post(
            f"/api/v1/capas/{capa_id}/grant", json={"permission": "knowledge:write"}, headers=admin
        )
        assert conflict.status_code == 409, conflict.text


def test_manifest_rejects_non_deferrable_permission() -> None:
    """Only permissions on the allow-list can be deferred: a capa must not be
    able to defer the consent its ordinary contributions run under."""
    with pytest.raises(ManifestError, match="deferred"):
        parse_manifest(
            {
                "name": "x",
                "version": "1.0.0",
                "permissions": ["mcp:connect"],
                "deferred_permissions": ["mcp:connect"],
            }
        )


@pytest.mark.asyncio
async def test_a_stored_manifest_cannot_defer_a_permission_off_the_allow_list(
    app_session: AppSessionFactory,
) -> None:
    """A stale or tampered stored manifest that claims `mcp:connect` deferred
    still leaves enabling waiting for `mcp:connect`."""
    from sqlalchemy.orm.attributes import flag_modified

    from oc8.capas.lifecycle import ConsentError, enable_plugin
    from oc8.capas.service import install_plugin

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        version = await install_plugin(
            db,
            tenant_id=tenant,
            manifest_data={"name": "tampered", "version": "1.0.0", "permissions": BASE},
        )
        version.manifest = {**version.manifest, "deferred_permissions": ["mcp:connect"]}
        flag_modified(version, "manifest")
        await db.flush()
        with pytest.raises(ConsentError, match="mcp:connect"):
            await enable_plugin(
                db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=["secrets:use"]
            )


@pytest.mark.asyncio
async def test_setting_up_a_never_enabled_capa_does_not_grant_a_deferred_permission(
    app_session: AppSessionFactory, capas_root: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    write_capa(
        capas_root,
        permissions=[*BASE, "knowledge:write"],
        deferred=["knowledge:write"],
        setup=True,
    )
    async with api() as c:
        r = await c.post(
            "/api/v1/capas/install-from-disk",
            json={"pluginId": NAME, "enable": False},
            headers=admin,
        )
        assert r.status_code == 201, r.text
        capa_id = r.json()["pluginId"]
        s = await c.post(
            f"/api/v1/capas/{capa_id}/setup", json={"values": {"label": "x"}}, headers=admin
        )
        assert s.status_code == 200, s.text
    inst = await _installation(app_session, tenant)
    assert inst.status == "enabled"
    assert sorted(inst.granted_permissions) == sorted(BASE)


@pytest.mark.asyncio
async def test_enable_never_grants_a_deferred_permission_and_never_drops_one(
    app_session: AppSessionFactory, capas_root: Path
) -> None:
    """Only POST /grant grants a deferred permission. An enable that asks for
    it does not get it, and a later enable (or an update's re-enable) keeps
    the one that was granted, even when the caller leaves it out."""
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    write_capa(capas_root, permissions=[*BASE, "knowledge:write"], deferred=["knowledge:write"])
    async with api() as c:
        capa_id = await _install(c, admin)

        asked = await c.post(
            f"/api/v1/capas/{capa_id}/enable",
            json={"grantedPermissions": [*BASE, "knowledge:write"]},
            headers=admin,
        )
        assert asked.status_code == 200, asked.text
        assert "knowledge:write" not in asked.json()["grantedPermissions"]
        assert (
            "knowledge:write" not in (await _installation(app_session, tenant)).granted_permissions
        )

        granted = await c.post(
            f"/api/v1/capas/{capa_id}/grant", json={"permission": "knowledge:write"}, headers=admin
        )
        assert granted.status_code == 200, granted.text
        assert "knowledge:write" in granted.json()["grantedPermissions"]

        again = await c.post(
            f"/api/v1/capas/{capa_id}/enable", json={"grantedPermissions": BASE}, headers=admin
        )
        assert again.status_code == 200, again.text
        assert "knowledge:write" in again.json()["grantedPermissions"]

        write_capa(
            capas_root,
            version="1.2.0",
            permissions=[*BASE, "knowledge:write"],
            deferred=["knowledge:write"],
        )
        await _install(c, admin)
        updated = await c.post(
            f"/api/v1/capas/{capa_id}/enable", json={"grantedPermissions": BASE}, headers=admin
        )
        assert updated.status_code == 200, updated.text
    inst = await _installation(app_session, tenant)
    assert inst.status == "enabled"
    assert inst.granted_permissions.count("knowledge:write") == 1
