"""GET/PATCH /mcp/connections/{id}/http-tools: the endpoints editor reads and
replaces an HTTP API connection's tools, read/modify classification and
import metadata -- validated, on every row of the same plugin connection,
and in the custom capa's own manifest so an enable cannot undo it."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.pdp import required_right
from oc8.capas.manifest import parse_manifest
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_TOOLS = [
    {
        "name": "list_tickets",
        "description": "List tickets.",
        "method": "GET",
        "url_template": "/tickets",
        "param_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
    {
        "name": "create_ticket",
        "description": "Create a ticket.",
        "method": "POST",
        "url_template": "/tickets",
        "param_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
    },
]

_MANIFEST: dict[str, Any] = {
    "name": "acme_tickets_api",
    "version": "1.0.0",
    "type": "tool_pack",
    "summary": "Acme tickets.",
    "tool_pack": {
        "connections": [
            {
                "key": "default",
                "name": "acme_tickets_api",
                "server_url": "https://tickets.acme.test",
                "transport": "manual_http",
                "scopes": {"read": ["list_tickets"], "modify": ["create_ticket"]},
                "config": {
                    "auth": {"kind": "api_key", "in": "query", "name": "api_key"},
                    "http_tools": _TOOLS,
                    "openapi": {
                        "source_url": "https://tickets.acme.test/openapi.json",
                        "fingerprint": "abc",
                        "imported_at": "2026-11-13T10:00:00Z",
                        "title": "Tickets",
                        "version": "1.0",
                    },
                },
            }
        ]
    },
    "setup": {
        "title": "Acme tickets",
        "fields": [],
        "mcp": {"connection_key": "default", "name": "acme_tickets_api"},
    },
}

_NEW_TOOLS = [
    {
        "name": "list_tickets",
        "description": "List open tickets. Use it to find a ticket id.",
        "method": "GET",
        "urlTemplate": "/tickets",
        "paramSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "paramLocations": {"q": "query"},
        "group": "tickets",
    },
    {
        "name": "get_ticket",
        "description": "Read one ticket.",
        "method": "GET",
        "urlTemplate": "/tickets/{id}",
        "paramSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
        "paramLocations": {"id": "path"},
    },
]


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _installed(c: AsyncClient, tenant: uuid.UUID) -> str:
    r = await c.post(
        "/api/v1/capas", json={"manifest": _MANIFEST, "origin": "custom"}, headers=_h(tenant)
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
                    .where(m.McpConnection.name == "acme_tickets_api")
                    .order_by(m.McpConnection.created_at)
                )
            ).scalars()
        )


def _patch_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "httpTools": _NEW_TOOLS,
        "scopes": {"read": ["get_ticket", "list_tickets"], "modify": []},
        "openapi": {
            "sourceUrl": "https://tickets.acme.test/openapi.json",
            "fingerprint": "def",
            "importedAt": "2026-11-14T09:00:00Z",
            "title": "Tickets",
            "version": "1.1",
        },
    }
    body.update(overrides)
    return body


async def test_get_reports_tools_scopes_login_and_metadata(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _installed(c, tenant)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.get(f"/api/v1/mcp/connections/{conn_id}/http-tools", headers=_h(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseUrl"] == "https://tickets.acme.test"
    assert [t["name"] for t in body["httpTools"]] == ["list_tickets", "create_ticket"]
    assert body["httpTools"][0]["urlTemplate"] == "/tickets"
    assert body["scopes"] == {"read": ["list_tickets"], "modify": ["create_ticket"]}
    assert body["auth"] == {
        "kind": "api_key",
        "in": "query",
        "name": "api_key",
        "username": None,
        "tokenUrl": None,
        "clientId": None,
        "scope": None,
    }
    assert body["openapi"]["sourceUrl"] == "https://tickets.acme.test/openapi.json"


async def test_a_legacy_auth_header_reads_as_an_api_key_header(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="legacy",
            server_url="https://legacy.acme.test",
            transport="manual_http",
            scopes=[],
            config={"auth_header_name": "Authorization", "http_tools": _TOOLS},
            connected=False,
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
        await db.commit()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        body = (
            await c.get(f"/api/v1/mcp/connections/{conn_id}/http-tools", headers=_h(tenant))
        ).json()
    assert body["auth"]["kind"] == "api_key"
    assert body["auth"]["in"] == "header"
    assert body["auth"]["name"] == "Authorization"
    assert body["scopes"] == {"read": [], "modify": []}
    assert body["openapi"] is None


async def test_patch_replaces_tools_scopes_and_metadata_everywhere(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oc8.agent.mcp_pool as mcp_pool

    closed: list[uuid.UUID] = []

    async def _close(connection_id: uuid.UUID) -> None:
        closed.append(connection_id)

    monkeypatch.setattr(mcp_pool, "close", _close)
    tenant = uuid.uuid4()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        plugin_id = await _installed(c, tenant)
        first = (await _rows(app_session, tenant))[0]
        # A second department's row of the same plugin connection.
        async with app_session(tenant) as db:
            sibling = m.McpConnection(
                tenant_id=tenant,
                name="acme_tickets_api",
                server_url=first.server_url,
                transport="manual_http",
                scopes=first.scopes,
                config=dict(first.config),
                connected=False,
            )
            db.add(sibling)
            await db.flush()
            sibling_id = sibling.id
            await db.commit()
        async with app_session(tenant) as db:
            before = (await db.execute(select(m.CapaVersion))).scalar_one().artifact_hash
        r = await c.patch(
            f"/api/v1/mcp/connections/{first.id}/http-tools", json=_patch_body(), headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
        assert [t["name"] for t in r.json()["httpTools"]] == ["list_tickets", "get_ticket"]

    for row in await _rows(app_session, tenant):
        assert [t["name"] for t in row.config["http_tools"]] == ["list_tickets", "get_ticket"]
        assert row.config["http_tools"][1]["url_template"] == "/tickets/{id}"
        assert row.config["http_tools"][1]["param_locations"] == {"id": "path"}
        assert row.config["http_tools"][0]["group"] == "tickets"
        assert row.config["openapi"]["fingerprint"] == "def"
        assert row.config["auth"] == {"kind": "api_key", "in": "query", "name": "api_key"}
        assert row.scopes == {"read": ["get_ticket", "list_tickets"], "modify": []}
    assert set(closed) == {first.id, sibling_id}

    async with app_session(tenant) as db:
        version = (await db.execute(select(m.CapaVersion))).scalar_one()
        assert str(version.capa_id) == plugin_id
    manifest_conn = parse_manifest(version.manifest).tool_pack.connections[0]
    assert [t["name"] for t in manifest_conn.config["http_tools"]] == ["list_tickets", "get_ticket"]
    assert manifest_conn.scopes == {"read": ["get_ticket", "list_tickets"], "modify": []}
    assert version.artifact_hash != before


async def test_reenabling_keeps_the_edited_tools_and_scopes(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        plugin_id = await _installed(c, tenant)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_id}/http-tools", json=_patch_body(), headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
        r = await c.post(
            f"/api/v1/capas/{plugin_id}/enable", json={"grantedPermissions": []}, headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
    row = (await _rows(app_session, tenant))[0]
    assert row.scopes == {"read": ["get_ticket", "list_tickets"], "modify": []}
    assert [t["name"] for t in row.config["http_tools"]] == ["list_tickets", "get_ticket"]


def _department_manifest(*, setup_scopes: list[str] | None = None) -> dict[str, Any]:
    """`_MANIFEST` with a department field, the way the wizard installs one
    for an organisation that has departments."""
    manifest = copy.deepcopy(_MANIFEST)
    manifest["setup"]["fields"] = [
        {"key": "department", "label": "Department", "kind": "department", "required": True}
    ]
    manifest["setup"]["mcp"]["department_field"] = "department"
    if setup_scopes is not None:
        manifest["setup"]["mcp"]["scopes"] = setup_scopes
    return manifest


async def _departments(app_session: AppSessionFactory, tenant: uuid.UUID, n: int) -> list[str]:
    ids: list[str] = []
    async with app_session(tenant) as db:
        for i in range(n):
            dept = m.Department(tenant_id=tenant, name=f"D{i}-{uuid.uuid4().hex}", frame={})
            db.add(dept)
            await db.flush()
            ids.append(str(dept.id))
        await db.commit()
    return ids


@pytest.mark.parametrize("setup_scopes", [None, ["list_tickets", "create_ticket"]])
async def test_second_department_gets_the_edited_tools(
    app_session: AppSessionFactory, setup_scopes: list[str] | None
) -> None:
    """A second department's row is seeded from the manifest's connection
    (api/v1/capas.py `_apply_plugin_setup`), and a setup form's own
    `setup.mcp.scopes` wins over the connection's: the edit has to reach both,
    or that department gets the old tools or the old classification."""
    tenant = uuid.uuid4()
    dept_a, dept_b = await _departments(app_session, tenant, 2)
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.post(
            "/api/v1/capas",
            json={"manifest": _department_manifest(setup_scopes=setup_scopes), "origin": "custom"},
            headers=_h(tenant),
        )
        assert r.status_code == 201, r.text
        plugin_id = r.json()["pluginId"]
        r = await c.post(
            f"/api/v1/capas/{plugin_id}/enable", json={"grantedPermissions": []}, headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
        r = await c.post(
            f"/api/v1/capas/{plugin_id}/setup",
            json={"values": {"department": dept_a}},
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
        conn_a = r.json()["connectionId"]
        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_a}/http-tools", json=_patch_body(), headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
        r = await c.post(
            f"/api/v1/capas/{plugin_id}/setup",
            json={"values": {"department": dept_b}},
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
        conn_b = r.json()["connectionId"]
    assert conn_b != conn_a
    async with app_session(tenant) as db:
        row_b = await db.get(m.McpConnection, uuid.UUID(conn_b))
        assert row_b is not None
        assert str(row_b.department_id) == dept_b
        assert [t["name"] for t in row_b.config["http_tools"]] == ["list_tickets", "get_ticket"]
        assert row_b.scopes == {"read": ["get_ticket", "list_tickets"], "modify": []}
        assert row_b.config["openapi"]["fingerprint"] == "def"


async def test_patch_without_the_connection_in_the_manifest_is_a_conflict(
    app_session: AppSessionFactory,
) -> None:
    """Rows changed but a manifest left behind would revert on the next
    enable: refuse instead of half-applying."""
    tenant = uuid.uuid4()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _installed(c, tenant)
        row = (await _rows(app_session, tenant))[0]
        async with app_session(tenant) as db:
            stored = await db.get(m.McpConnection, row.id)
            assert stored is not None
            stored.config = {**stored.config, "_connection_key": "other"}
            await db.commit()
        r = await c.patch(
            f"/api/v1/mcp/connections/{row.id}/http-tools", json=_patch_body(), headers=_h(tenant)
        )
    assert r.status_code == 409, r.text
    assert "manifest" in r.json()["detail"]
    after = (await _rows(app_session, tenant))[0]
    assert [t["name"] for t in after.config["http_tools"]] == ["list_tickets", "create_ticket"]


async def test_pooled_sessions_close_after_the_edit_is_committed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session reopened between the close and the commit would serve the old
    tools until it ages out: the close comes after the commit."""
    import oc8.agent.mcp_pool as mcp_pool

    seen: list[list[str]] = []
    tenant = uuid.uuid4()

    async def _close(connection_id: uuid.UUID) -> None:
        async with app_session(tenant) as db:
            row = await db.get(m.McpConnection, connection_id)
            assert row is not None
            seen.append([t["name"] for t in row.config["http_tools"]])

    monkeypatch.setattr(mcp_pool, "close", _close)
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _installed(c, tenant)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_id}/http-tools", json=_patch_body(), headers=_h(tenant)
        )
        assert r.status_code == 200, r.text
    assert seen == [["list_tickets", "get_ticket"]]


async def test_scope_lists_are_deduplicated(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _installed(c, tenant)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_id}/http-tools",
            json=_patch_body(scopes={"read": ["list_tickets", "list_tickets"], "modify": []}),
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
    assert (await _rows(app_session, tenant))[0].scopes == {"read": ["list_tickets"], "modify": []}


async def test_get_tolerates_non_string_metadata_and_login_values(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="odd",
            server_url="https://odd.acme.test",
            transport="manual_http",
            scopes={"read": ["list_tickets", 7], "modify": "create_ticket"},
            config={
                "auth": {"kind": "api_key", "in": 1, "name": ["x"], "username": None},
                "openapi": {"fingerprint": 123, "imported_at": None, "title": {"a": 1}},
                "http_tools": _TOOLS,
            },
            connected=False,
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
        await db.commit()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.get(f"/api/v1/mcp/connections/{conn_id}/http-tools", headers=_h(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["auth"]["kind"] == "api_key"
    assert body["auth"]["in"] == "1"
    assert body["openapi"]["fingerprint"] == "123"
    assert body["openapi"]["importedAt"] == ""
    assert body["scopes"] == {"read": ["list_tickets", "7"], "modify": []}


async def test_unclassified_tools_stay_modify(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _installed(c, tenant)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_id}/http-tools",
            json=_patch_body(scopes={"read": ["list_tickets"], "modify": []}),
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
    row = (await _rows(app_session, tenant))[0]
    assert required_right("get_ticket", row.scopes) == "modify"
    assert required_right("list_tickets", row.scopes) == "read"


async def test_openapi_omitted_keeps_null_removes(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _installed(c, tenant)
        conn_id = (await _rows(app_session, tenant))[0].id
        body = _patch_body()
        del body["openapi"]
        await c.patch(
            f"/api/v1/mcp/connections/{conn_id}/http-tools", json=body, headers=_h(tenant)
        )
        assert (await _rows(app_session, tenant))[0].config["openapi"]["fingerprint"] == "abc"
        await c.patch(
            f"/api/v1/mcp/connections/{conn_id}/http-tools",
            json=_patch_body(openapi=None),
            headers=_h(tenant),
        )
    assert "openapi" not in (await _rows(app_session, tenant))[0].config


def _tool(**overrides: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": "t1",
        "method": "GET",
        "urlTemplate": "/x",
        "paramSchema": {"type": "object", "properties": {}},
    }
    tool.update(overrides)
    return tool


def _nested(depth: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    for _ in range(depth):
        schema = {"type": "object", "properties": {"x": schema}}
    return schema


def _huge_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {f"p{i}": {"type": "string", "description": "x" * 100} for i in range(700)},
    }


def test_a_local_ref_is_accepted() -> None:
    from oc8.schemas.requests import HttpToolSpec

    HttpToolSpec.model_validate(
        _tool(
            paramSchema={
                "type": "object",
                "$defs": {"id": {"type": "string"}},
                "properties": {"id": {"$ref": "#/$defs/id"}},
            }
        )
    )


@pytest.mark.parametrize(
    ("tools", "scopes"),
    [
        ([_tool(), _tool()], {"read": [], "modify": []}),
        ([_tool(name="x" * 65)], {"read": [], "modify": []}),
        ([_tool(name="list tickets")], {"read": [], "modify": []}),
        ([_tool(paramSchema={"type": "nope"})], {"read": [], "modify": []}),
        ([_tool(paramSchema={"type": "string"})], {"read": [], "modify": []}),
        ([_tool(urlTemplate="https://evil.test/x")], {"read": [], "modify": []}),
        ([_tool(urlTemplate="//evil.test/x")], {"read": [], "modify": []}),
        ([_tool()], {"read": ["ghost"], "modify": []}),
        ([_tool()], {"read": ["t1"], "modify": ["t1"]}),
        ([_tool(paramSchema=_nested(40))], {"read": [], "modify": []}),
        ([_tool(paramSchema=_nested(2000))], {"read": [], "modify": []}),
        ([_tool(paramSchema=_huge_schema())], {"read": [], "modify": []}),
        (
            [_tool(paramSchema={"type": "object", "$ref": "http://169.254.169.254/x"})],
            {"read": [], "modify": []},
        ),
        (
            [
                _tool(
                    paramSchema={
                        "type": "object",
                        "properties": {"a": {"$ref": "https://evil.test/s.json#/a"}},
                    }
                )
            ],
            {"read": [], "modify": []},
        ),
        ([_tool(name=f"t{i}") for i in range(2001)], {"read": [], "modify": []}),
    ],
)
async def test_validation(
    app_session: AppSessionFactory, tools: list[dict[str, Any]], scopes: dict[str, list[str]]
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _installed(c, tenant)
        conn_id = (await _rows(app_session, tenant))[0].id
        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_id}/http-tools",
            json={"httpTools": tools, "scopes": scopes},
            headers=_h(tenant),
        )
    assert r.status_code == 422, r.text


async def test_permission_tenant_and_transport_guards(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        stdio = m.McpConnection(
            tenant_id=tenant,
            name="s",
            server_url="",
            transport="stdio",
            scopes=[],
            config={},
            connected=False,
        )
        db.add(stdio)
        await db.flush()
        stdio_id = stdio.id
        await db.commit()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _installed(c, tenant)
        conn_id = (await _rows(app_session, tenant))[0].id
        url = f"/api/v1/mcp/connections/{conn_id}/http-tools"
        assert (await c.get(url, headers=_h(tenant, "member"))).status_code == 403
        assert (
            await c.patch(url, json=_patch_body(), headers=_h(tenant, "member"))
        ).status_code == 403
        assert (await c.get(url, headers=_h(uuid.uuid4()))).status_code == 404
        assert (
            await c.patch(
                f"/api/v1/mcp/connections/{stdio_id}/http-tools",
                json=_patch_body(),
                headers=_h(tenant),
            )
        ).status_code == 409


async def test_a_non_custom_plugins_tools_are_not_editable(app_session: AppSessionFactory) -> None:
    from oc8.capas.service import install_plugin

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await install_plugin(db, tenant_id=tenant, manifest_data=_MANIFEST, origin="local")
        row = m.McpConnection(
            tenant_id=tenant,
            name="acme_tickets_api",
            server_url="https://tickets.acme.test",
            transport="manual_http",
            scopes={},
            config={
                "_plugin_name": "acme_tickets_api",
                "_connection_key": "default",
                "http_tools": _TOOLS,
            },
            connected=False,
        )
        db.add(row)
        await db.flush()
        conn_id = row.id
        await db.commit()
    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_id}/http-tools", json=_patch_body(), headers=_h(tenant)
        )
    assert r.status_code == 409
