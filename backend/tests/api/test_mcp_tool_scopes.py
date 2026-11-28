"""GET/PUT /mcp/connections/{id}/tool-scopes: which of a connection's tools
only read and which modify, for a connection whose tools come from its own
server (stdio, SSE, streamable HTTP) rather than an HTTP API description.

Without a classification every tool requires `modify` (fail-closed), so a
read-only guardrail left such a connection with no tools at all: the person
could not say that `mail_search` only reads. The classification is written to
every row of the same plugin connection and to the custom capa's manifest, so
neither a further department nor an enable undoes it."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import _mcp_auth
from oc8.auth import get_identity_provider
from oc8.authz.pdp import required_right
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_TOOLS = ["mail_search", "mail_get", "mail_list_folders", "mail_create_draft"]

_MANIFEST: dict[str, Any] = {
    "name": "shared_mailboxes",
    "version": "1.0.0",
    "type": "tool_pack",
    "summary": "Shared mailboxes.",
    "tool_pack": {
        "connections": [
            {
                "key": "default",
                "name": "shared_mailboxes",
                "server_url": "",
                "transport": "stdio",
                "scopes": [],
                "config": {"command": "python", "args": ["-m", "mailboxes"]},
            }
        ]
    },
    "setup": {
        "title": "Shared mailboxes",
        "fields": [],
        "mcp": {"connection_key": "default", "name": "shared_mailboxes"},
    },
}


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _installed(
    c: AsyncClient, tenant: uuid.UUID, manifest: dict[str, Any] = _MANIFEST
) -> str:
    r = await c.post(
        "/api/v1/capas", json={"manifest": manifest, "origin": "custom"}, headers=_h(tenant)
    )
    assert r.status_code == 201, r.text
    plugin_id = r.json()["pluginId"]
    r = await c.post(
        f"/api/v1/capas/{plugin_id}/enable", json={"grantedPermissions": []}, headers=_h(tenant)
    )
    assert r.status_code == 200, r.text
    return plugin_id


async def _rows(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.McpConnection]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.McpConnection)
                    .where(m.McpConnection.name == "shared_mailboxes")
                    .order_by(m.McpConnection.created_at)
                )
            ).scalars()
        )


async def _tested(app_session: AppSessionFactory, tenant: uuid.UUID, tools: list[str]) -> None:
    """What a successful connection test records: the tools the server offers."""
    async with app_session(tenant) as db:
        for row in (
            await db.execute(
                select(m.McpConnection).where(m.McpConnection.name == "shared_mailboxes")
            )
        ).scalars():
            row.health = {"status": "ok", "toolCount": len(tools), "tools": list(tools)}
        await db.commit()


async def _plain(app_session: AppSessionFactory, tenant: uuid.UUID, **kw: Any) -> uuid.UUID:
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name=kw.pop("name", "plain"),
            server_url=kw.pop("server_url", ""),
            transport=kw.pop("transport", "stdio"),
            scopes=kw.pop("scopes", []),
            config=kw.pop("config", {"command": "python"}),
            connected=True,
            health={"status": "ok", "tools": list(_TOOLS), "toolCount": len(_TOOLS)},
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
        await db.commit()
    return conn_id


_SPLIT = {"read": ["mail_search", "mail_get", "mail_list_folders"], "modify": ["mail_create_draft"]}


async def test_get_lists_the_servers_tools_and_none_are_classified_yet(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _installed(c, tenant)
        await _tested(app_session, tenant, _TOOLS)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.get(f"/api/v1/mcp/connections/{conn_id}/tool-scopes", headers=_h(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tools"] == sorted(_TOOLS)
    assert body["read"] == [] and body["modify"] == []
    assert body["editable"] is True


async def test_put_classifies_every_department_and_the_engine_reads_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _installed(c, tenant)
        first = (await _rows(app_session, tenant))[0]
        async with app_session(tenant) as db:
            db.add(
                m.McpConnection(
                    tenant_id=tenant,
                    name="shared_mailboxes",
                    server_url="",
                    transport="stdio",
                    scopes=[],
                    config=dict(first.config),
                    connected=False,
                )
            )
            await db.commit()
        await _tested(app_session, tenant, _TOOLS)
        r = await c.put(
            f"/api/v1/mcp/connections/{first.id}/tool-scopes", json=_SPLIT, headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
        assert r.json()["read"] == sorted(_SPLIT["read"])

    rows = await _rows(app_session, tenant)
    assert len(rows) == 2
    for row in rows:
        scopes = _mcp_auth(row).tool_scopes
        assert required_right("mail_search", scopes) == "read"
        assert required_right("mail_get", scopes) == "read"
        assert required_right("mail_list_folders", scopes) == "read"
        assert required_right("mail_create_draft", scopes) == "modify"


async def test_reenabling_the_plugin_keeps_the_classification(
    app_session: AppSessionFactory,
) -> None:
    """Enabling rewrites each row's scopes from the manifest. Kept only on the
    rows, the classification would silently revert to "everything modifies"."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        plugin_id = await _installed(c, tenant)
        await _tested(app_session, tenant, _TOOLS)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes", json=_SPLIT, headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
        r = await c.post(
            f"/api/v1/capas/{plugin_id}/enable", json={"grantedPermissions": []}, headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
    for row in await _rows(app_session, tenant):
        assert required_right("mail_search", _mcp_auth(row).tool_scopes) == "read"


async def test_a_tool_left_out_or_new_stays_modify(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await _installed(c, tenant)
        await _tested(app_session, tenant, _TOOLS)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes",
            json={"read": ["mail_search"], "modify": []},
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
    (row,) = await _rows(app_session, tenant)
    scopes = _mcp_auth(row).tool_scopes
    assert required_right("mail_search", scopes) == "read"
    assert required_right("mail_get", scopes) == "modify"
    assert required_right("mail_delete_everything", scopes) == "modify"


async def test_a_tool_in_both_lists_is_stored_as_modify(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    conn_id = await _plain(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes",
            json={"read": ["mail_get", "mail_get", "mail_search"], "modify": ["mail_get"]},
            headers=_h(tenant),
        )
    assert r.status_code == 200, r.text
    assert r.json()["read"] == ["mail_search"]
    assert r.json()["modify"] == ["mail_get"]


async def test_a_connection_outside_any_plugin_is_classified_on_its_row(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    conn_id = await _plain(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes", json=_SPLIT, headers=_h(tenant)
        )
    assert r.status_code == 200, r.text
    async with app_session(tenant) as db:
        row = await db.get(m.McpConnection, conn_id)
        assert row is not None
        assert required_right("mail_get", _mcp_auth(row).tool_scopes) == "read"


async def test_an_unknown_tool_name_is_refused(app_session: AppSessionFactory) -> None:
    """A typo would classify nothing while looking done."""
    tenant = uuid.uuid4()
    conn_id = await _plain(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes",
            json={"read": ["mail_serach"], "modify": []},
            headers=_h(tenant),
        )
    assert r.status_code == 422, r.text


async def test_a_connection_never_tested_has_no_tools_to_classify(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="untested",
            server_url="",
            transport="stdio",
            scopes=[],
            config={"command": "python"},
            connected=False,
            health={},
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
        await db.commit()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.get(f"/api/v1/mcp/connections/{conn_id}/tool-scopes", headers=_h(tenant))
    assert r.status_code == 200, r.text
    assert r.json()["tools"] == []


async def test_an_http_api_connection_uses_its_endpoints_editor(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    conn_id = await _plain(
        app_session,
        tenant,
        transport="manual_http",
        server_url="https://x.test",
        config={"http_tools": []},
    )
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes", json=_SPLIT, headers=_h(tenant)
        )
    assert r.status_code == 409, r.text


async def test_a_plugins_own_classification_is_not_editable(
    app_session: AppSessionFactory,
) -> None:
    """A plugin that is not the tenant's own classifies its tools itself."""
    tenant = uuid.uuid4()
    conn_id = await _plain(
        app_session, tenant, config={"command": "python", "_plugin_name": "shipped_pack"}
    )
    async with app_session(tenant) as db:
        db.add(m.Capa(tenant_id=tenant, name="shipped_pack", type="tool_pack", origin="local"))
        await db.commit()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        got = await c.get(f"/api/v1/mcp/connections/{conn_id}/tool-scopes", headers=_h(tenant))
        r = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes", json=_SPLIT, headers=_h(tenant)
        )
    assert got.status_code == 200 and got.json()["editable"] is False
    assert r.status_code == 409, r.text


async def test_permission_and_tenant_guards(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    conn_id = await _plain(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        other = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes", json=_SPLIT, headers=_h(uuid.uuid4())
        )
        member = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes",
            json=_SPLIT,
            headers=_h(tenant, role="member"),
        )
    assert other.status_code == 404
    assert member.status_code == 403


async def test_the_guardrail_editor_sees_a_classified_connections_tools(
    app_session: AppSessionFactory,
) -> None:
    """The guardrail editor reads a connection's tools by name; a connection
    without a shipped manifest used to come back empty there."""
    tenant = uuid.uuid4()
    conn_id = await _plain(app_session, tenant, name="mailboxes")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.put(
            f"/api/v1/mcp/connections/{conn_id}/tool-scopes", json=_SPLIT, headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
        r = await c.get("/api/v1/mcp/connections/mailboxes/tool-names", headers=_h(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["names"] == sorted(_TOOLS)
    assert body["read"] == sorted(_SPLIT["read"])
    assert body["modify"] == ["mail_create_draft"]
