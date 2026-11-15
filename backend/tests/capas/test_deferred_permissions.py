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
    with pytest.raises(ManifestError, match="deferred_permissions"):
        parse_manifest(
            {"name": "x", "version": "1.0.0", "permissions": ["a"], "deferred_permissions": ["b"]}
        )
    ok = parse_manifest(
        {"name": "x", "version": "1.0.0", "permissions": ["a", "b"], "deferred_permissions": ["b"]}
    )
    assert ok.deferred_permissions == ["b"]


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
