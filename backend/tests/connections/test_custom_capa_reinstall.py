"""A capa from the custom server wizard: its name cannot be a shipped
plugin's, and an existing version of it is never replaced by a reinstall."""

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


async def test_a_disabled_custom_capas_version_is_not_replaced_by_a_reinstall(
    app_session: AppSessionFactory,
) -> None:
    # A cancelled wizard left "billing_api" disabled. A new wizard under the
    # same name must not take it over: its setup would reuse the old row's
    # server_url and send the new key to the old host.
    tenant = uuid.uuid4()
    async with api() as c:
        first = await _install(c, tenant, _manifest("1.0.0"))
        assert first.status_code == 201, first.text
        capa_id = first.json()["pluginId"]
        await _enable(c, tenant, capa_id)
        await _disable(c, tenant, capa_id)
        other = _manifest("1.0.0", summary="Another vendor.")
        other["tool_pack"]["connections"][0]["server_url"] = "https://other.example.com"
        r = await _install(c, tenant, other)
    assert r.status_code == 400
    assert "already installed" in r.json()["detail"]
    async with app_session(tenant) as db:
        version = (await db.execute(select(m.CapaVersion))).scalar_one()
        assert version.manifest["summary"] == "Billing."
        connection = version.manifest["tool_pack"]["connections"][0]
        assert connection["server_url"] == "https://billing.example.com"


async def test_a_custom_install_cannot_add_a_version_to_a_capa_from_disk(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Capa(tenant_id=tenant, name=NAME, type="tool_pack", origin="local"))
    async with api() as c:
        r = await _install(c, tenant, _manifest("1.0.0"))
    assert r.status_code == 400
