"""Shared helpers for the connections tests: tokens for named people, a fresh
app client, and rows created directly in a tenant."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.capas.discovery import find_plugin
from oc8.main import create_app
from tests.conftest import AppSessionFactory


def headers(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def api() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            yield client


async def install(client: AsyncClient, admin: dict[str, str], plugin: str) -> str:
    """Install a plugin from disk and enable it with the permissions its
    manifest asks for; returns the capa id."""
    r = await client.post(
        "/api/v1/capas/install-from-disk", json={"pluginId": plugin}, headers=admin
    )
    assert r.status_code in (200, 201), r.text
    capa_id = str(r.json()["pluginId"])
    found = find_plugin(plugin)
    assert found is not None and found.manifest is not None
    perms = list(found.manifest.get("permissions", []))
    enabled = await client.post(
        f"/api/v1/capas/{capa_id}/enable", json={"grantedPermissions": perms}, headers=admin
    )
    assert enabled.status_code == 200, enabled.text
    return capa_id


async def member(
    app_session: AppSessionFactory, tenant: uuid.UUID, subject: str, *, name: str | None = None
) -> uuid.UUID:
    async with app_session(tenant) as db:
        row = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=uuid.uuid4(),
            display_name=name or subject,
            email=subject,
        )
        db.add(row)
        await db.flush()
        return row.id


async def oauth_row(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    owner: uuid.UUID | None,
    provider: str = "microsoft",
    label: str = "anna@acme.test",
    status: str = "active",
    grant_type: str = "authorization_code",
) -> uuid.UUID:
    async with app_session(tenant) as db:
        row = m.OAuthConnection(
            tenant_id=tenant,
            provider=provider,
            account_label=label,
            scopes=[],
            access_secret_ref="",
            status=status,
            client_source="platform",
            grant_type=grant_type,
            owner_member_id=owner,
        )
        db.add(row)
        await db.flush()
        return row.id


async def credential_row(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    owner: uuid.UUID | None,
    credential_type: str = "odoo_login",
    name: str = "Odoo",
) -> uuid.UUID:
    async with app_session(tenant) as db:
        row = m.Credential(
            tenant_id=tenant,
            name=name,
            credential_type=credential_type,
            field_values={"url": "https://odoo.acme.test", "username": "anna"},
            secret_refs={},
            owner_member_id=owner,
        )
        db.add(row)
        await db.flush()
        return row.id


async def enable_plugin_row(app_session: AppSessionFactory, tenant: uuid.UUID, name: str) -> None:
    """Mark a plugin installed and enabled for the tenant (rows only, no
    materialisation): the runtime refuses integrations of other plugins."""
    async with app_session(tenant) as db:
        if await db.scalar(select(m.Capa).where(m.Capa.name == name)) is not None:
            return
        capa = m.Capa(tenant_id=tenant, name=name, type="tool_pack")
        db.add(capa)
        await db.flush()
        db.add(m.CapaInstallation(tenant_id=tenant, capa_id=capa.id, status="enabled"))
        await db.flush()
