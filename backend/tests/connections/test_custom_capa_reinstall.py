"""A capa from the custom server wizard: its name cannot be a shipped
plugin's, and a version left behind by a cancelled wizard does not block the
next wizard under the same name."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers

pytestmark = pytest.mark.asyncio

NAME = "billing_api"


def _manifest(version: str, *, name: str = NAME, summary: str = "Billing.") -> dict[str, Any]:
    return {
        "name": name,
        "version": version,
        "type": "tool_pack",
        "summary": summary,
        "tool_pack": {
            "connections": [
                {
                    "key": "default",
                    "name": name,
                    "server_url": "https://billing.example.com",
                    "transport": "manual_http",
                    "config": {
                        "http_tools": [
                            {"name": "list_invoices", "method": "GET", "url_template": "/invoices"}
                        ]
                    },
                }
            ]
        },
    }


def _admin(tenant: uuid.UUID) -> dict[str, str]:
    return headers(tenant, "admin@acme.test", "org_admin")


async def _install(c: Any, tenant: uuid.UUID, manifest: dict[str, Any]) -> Any:
    return await c.post(
        "/api/v1/capas",
        json={"manifest": manifest, "origin": "custom", "trustLevel": "community"},
        headers=_admin(tenant),
    )


async def _enable(c: Any, tenant: uuid.UUID, capa_id: str) -> None:
    r = await c.post(
        f"/api/v1/capas/{capa_id}/enable", json={"grantedPermissions": []}, headers=_admin(tenant)
    )
    assert r.status_code == 200, r.text


async def _disable(c: Any, tenant: uuid.UUID, capa_id: str) -> None:
    r = await c.post(
        f"/api/v1/capas/{capa_id}/disable", json={"reason": "cancelled"}, headers=_admin(tenant)
    )
    assert r.status_code == 200, r.text


async def test_a_custom_capa_cannot_take_a_shipped_plugins_name(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with api() as c:
        r = await _install(c, tenant, _manifest("1.0.0", name="github_mcp"))
    assert r.status_code == 400
    assert "github_mcp" in r.json()["detail"]
    async with app_session(tenant) as db:
        assert (await db.execute(select(m.Capa))).scalars().all() == []


async def test_a_cancelled_wizards_versions_are_replaced_by_the_next_one(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with api() as c:
        # First wizard: tested twice (1.0.0, then 1.0.1), then "Disable & cancel".
        first = await _install(c, tenant, _manifest("1.0.0"))
        assert first.status_code == 201, first.text
        capa_id = first.json()["pluginId"]
        await _enable(c, tenant, capa_id)
        r = await _install(c, tenant, _manifest("1.0.1"))
        assert r.status_code == 201, r.text
        await _enable(c, tenant, capa_id)
        await _disable(c, tenant, capa_id)

        # Second wizard, same name: starts at 1.0.0 again.
        again = await _install(c, tenant, _manifest("1.0.0", summary="Billing, second try."))
        assert again.status_code == 201, again.text
        assert again.json()["pluginId"] == capa_id
        await _enable(c, tenant, capa_id)
        # Its next test installs 1.0.1, which the first wizard left behind.
        r = await _install(c, tenant, _manifest("1.0.1", summary="Billing, third try."))
        assert r.status_code == 201, r.text

    async with app_session(tenant) as db:
        capa = await db.get(m.Capa, uuid.UUID(capa_id))
        assert capa is not None
        current = await db.get(m.CapaVersion, capa.current_version_id)
        assert current is not None
        assert current.semver == "1.0.1"
        assert current.manifest["summary"] == "Billing, third try."
        versions = (
            await db.execute(select(m.CapaVersion).where(m.CapaVersion.capa_id == capa.id))
        ).scalars()
        assert sorted(v.semver for v in versions) == ["1.0.0", "1.0.1"]


async def test_the_live_version_of_an_enabled_custom_capa_is_not_replaced(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with api() as c:
        first = await _install(c, tenant, _manifest("1.0.0"))
        assert first.status_code == 201, first.text
        capa_id = first.json()["pluginId"]
        await _enable(c, tenant, capa_id)
        r = await _install(c, tenant, _manifest("1.0.0", summary="Someone else's."))
        assert r.status_code == 400
        assert "already installed" in r.json()["detail"]
        # Saved at 1.0.1: a new wizard under the name starts at 1.0.0 again.
        r = await _install(c, tenant, _manifest("1.0.1"))
        assert r.status_code == 201, r.text
        await _enable(c, tenant, capa_id)
        r = await _install(c, tenant, _manifest("1.0.0", summary="Someone else's."))
        assert r.status_code == 400
    async with app_session(tenant) as db:
        versions = (await db.execute(select(m.CapaVersion))).scalars().all()
        assert {v.manifest["summary"] for v in versions} == {"Billing."}


async def test_a_custom_install_cannot_add_a_version_to_a_capa_from_disk(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Capa(tenant_id=tenant, name=NAME, type="tool_pack", origin="local"))
    async with api() as c:
        r = await _install(c, tenant, _manifest("1.0.0"))
    assert r.status_code == 400
