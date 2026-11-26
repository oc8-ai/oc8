"""GET /mcp/connections carries each connection's guardrail presets (§ plugin
guardrail presets, task 4): a connection materialised from a plugin that ships
presets returns them and says whether its manifest declares a `value_spec`; a
connection with no known plugin returns neither, and the request must not
fail because of it."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# tests/api/<this> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _plugins_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_PLUGINS_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _odoo_mcp_connection(db: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    """An McpConnection row wired to the odoo_mcp plugin's "primary" tool-pack
    connection, the way `materialise.py` stamps one -- not the shape a plugin
    author writes in the manifest's `config` table, just the identifying pair
    `_plugin_name`/`_connection_key` this endpoint resolves by."""
    conn = m.McpConnection(
        tenant_id=tenant,
        name="odoo",
        server_url="",
        transport="stdio",
        scopes=[],
        config={"_plugin_name": "odoo_mcp", "_connection_key": "primary"},
        connected=False,
    )
    db.add(conn)
    await db.flush()
    return conn.id


async def _gitea_mcp_connection(db: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    """An McpConnection row wired to gitea_mcp's "primary" tool-pack
    connection -- a real, installed tool_pack plugin that (unlike odoo_mcp)
    ships no `guardrails/` folder at all, exercising the "plugin found, no library"
    branch rather than the "plugin not found at all" branch the ghost/custom
    fixtures already cover."""
    conn = m.McpConnection(
        tenant_id=tenant,
        name="gitea",
        server_url="",
        transport="stdio",
        scopes=[],
        config={"_plugin_name": "gitea_mcp", "_connection_key": "primary"},
        connected=False,
    )
    db.add(conn)
    await db.flush()
    return conn.id


async def _custom_mcp_connection(db: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    """A bare connection tied to no known plugin, as an operator-created one
    (POST /mcp/connections) always is."""
    conn = m.McpConnection(
        tenant_id=tenant,
        name="custom",
        server_url="",
        transport="stdio",
        scopes=[],
        config={"command": "true", "args": []},
        connected=False,
    )
    db.add(conn)
    await db.flush()
    return conn.id


async def test_odoo_connection_carries_its_guardrail_presets(
    app_session: AppSessionFactory,
) -> None:
    assert _PLUGINS_DIR.is_dir(), _PLUGINS_DIR
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _odoo_mcp_connection(db, tenant)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            conn = next(x for x in resp.json() if x["id"] == str(conn_id))
            keys = {p["key"] for p in conn["guardrailPresets"]}
            assert keys == {
                "read_only",
                "assist_with_approval",
                "autonomous_with_limit",
                "internal_only",
                "no_deletions",
            }
            recommended = [p["key"] for p in conn["guardrailPresets"] if p["recommended"]]
            assert recommended == ["assist_with_approval"]
            assert conn["hasValueSpec"] is True


async def test_odoo_connection_carries_its_guardrail_library(
    app_session: AppSessionFactory,
) -> None:
    """GET /mcp/connections also carries the plugin's curated library -- the
    28 `kind = "library"` files in `plugins/odoo_mcp/guardrails/` -- alongside,
    not instead of, the generic `guardrailPresets` asserted above (which are
    the `kind = "preset"` files in that same folder)."""
    assert _PLUGINS_DIR.is_dir(), _PLUGINS_DIR
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _odoo_mcp_connection(db, tenant)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            conn = next(x for x in resp.json() if x["id"] == str(conn_id))
            library = conn["guardrailLibrary"]
            assert library is not None
            entries = {g["key"]: g for g in library}
            assert "quote_approval_threshold" in entries
            quote = entries["quote_approval_threshold"]
            assert quote["useCase"] == "sales"
            assert quote["read"] is True
            assert quote["modify"] is True
            assert quote["approvalEur"] == 3000
            adjustable = {a["field"] for a in quote["adjustable"]}
            assert "approval_eur" in adjustable
            # The existing generic presets keep serialising unchanged.
            assert {p["key"] for p in conn["guardrailPresets"]}


async def test_gitea_connection_has_no_guardrail_library(
    app_session: AppSessionFactory,
) -> None:
    """A real, installed plugin that simply ships no `guardrails/` folder
    (unlike odoo_mcp) must report `guardrailLibrary: null`, not an error and not
    an empty list standing in for "none"."""
    assert _PLUGINS_DIR.is_dir(), _PLUGINS_DIR
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _gitea_mcp_connection(db, tenant)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            conn = next(x for x in resp.json() if x["id"] == str(conn_id))
            assert conn["guardrailLibrary"] is None


async def test_unmanaged_connection_has_no_presets(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _custom_mcp_connection(db, tenant)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            conn = next(x for x in resp.json() if x["id"] == str(conn_id))
            assert conn["guardrailPresets"] == []
            assert conn["hasValueSpec"] is False


async def test_odoo_connection_carries_its_credential_type(
    app_session: AppSessionFactory,
) -> None:
    """The manifest's `ToolPackConnection.credential_type` (odoo_mcp's
    tool_pack.toml declares "odoo_login") rides along on GET /mcp/connections
    -- lets a "New login" flow pick the right credential type automatically
    instead of asking the operator to choose from every registered type
    (live user feedback: picking from an unrelated s3_api entry "macht
    keinen Sinn"). Resolved from disk exactly like guardrailPresets, so a
    connection tied to a plugin that doesn't declare one -- or is no longer
    installed -- must report null, not an error."""
    assert _PLUGINS_DIR.is_dir(), _PLUGINS_DIR
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _odoo_mcp_connection(db, tenant)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            conn = next(x for x in resp.json() if x["id"] == str(conn_id))
            assert conn["credentialType"] == "odoo_login"


async def test_unmanaged_connection_has_no_credential_type(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _custom_mcp_connection(db, tenant)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            conn = next(x for x in resp.json() if x["id"] == str(conn_id))
            assert conn["credentialType"] is None


async def test_connection_for_a_removed_plugin_has_no_presets(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection materialised by a plugin that has since been uninstalled
    from disk must still list -- with empty presets, not a 500."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="ghost",
            server_url="",
            transport="stdio",
            scopes=[],
            config={"_plugin_name": "no_such_plugin", "_connection_key": "primary"},
            connected=False,
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            found = next(x for x in resp.json() if x["id"] == str(conn_id))
            assert found["guardrailPresets"] == []
            assert found["hasValueSpec"] is False


async def test_the_tool_allowlist_survives_the_dto_boundary(
    app_session: AppSessionFactory,
) -> None:
    """The safety of `autonomous_with_limit` IS its allowlist.

    That preset withholds `delete_record`, because a deletion carries no amount
    and so can never meet its EUR 1000 threshold -- `authorize_tool` counts an
    unreadable value as zero. A DTO that dropped `only` would hand the browser
    "read+write+send above 1000, everything reachable", which is the unattended
    deletion configuration, under a name promising a limit. The property is
    only worth anything if it survives serialisation, so this asserts it on the
    wire rather than in the manifest.
    """
    assert _PLUGINS_DIR.is_dir(), _PLUGINS_DIR
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _odoo_mcp_connection(db, tenant)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            conn = next(x for x in resp.json() if x["id"] == str(conn_id))
            presets = {p["key"]: p for p in conn["guardrailPresets"]}

    autonomous = presets["autonomous_with_limit"]
    assert autonomous["only"], "an empty allowlist means every tool, including delete_record"
    assert "delete_record" not in autonomous["only"]
    assert {"create_record", "update_record"} <= set(autonomous["only"])

    # The contrast: every send already reaches a person there, so nothing is
    # withheld. If this ever gains an allowlist the two presets have converged.
    assert presets["assist_with_approval"]["only"] == []


async def test_get_connection_tool_names_returns_the_connections_own_tools(
    app_session: AppSessionFactory,
) -> None:
    assert _PLUGINS_DIR.is_dir(), _PLUGINS_DIR
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _odoo_mcp_connection(db, tenant)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections/odoo/tool-names", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            names = body["names"]
            assert "search_records" in names
            assert "create_record" in names
            # Split by right, not just the flat union: a guardrail editor needs
            # to know which single checkbox (read xor modify) applies to a
            # given tool -- a tool is never both.
            assert "search_records" in body["read"]
            assert "search_records" not in body["modify"]
            assert "create_record" in body["modify"]
            assert "create_record" not in body["read"]


async def test_get_connection_tool_names_for_an_unknown_connection_is_empty(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(
                "/api/v1/mcp/connections/does-not-exist/tool-names", headers=_h(tenant)
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["names"] == []


async def test_a_connection_lists_its_outward_tools(app_session: AppSessionFactory) -> None:
    """The UI tells a preset meant for an autonomous agent (outward tools
    allowed unasked) by these: such a preset only works once the agent is
    autonomous with the outward tier granted (E2E F2)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        plain_id = await _custom_mcp_connection(db, tenant)
        mail = m.McpConnection(
            tenant_id=tenant,
            name="mail",
            server_url="",
            transport="stdio",
            scopes=[],
            config={"command": "true", "args": [], "outward_tools": ["mail_send"]},
            connected=False,
        )
        db.add(mail)
        await db.flush()
        mail_id = mail.id
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            by_id = {x["id"]: x for x in resp.json()}
            assert by_id[str(mail_id)]["outwardTools"] == ["mail_send"]
            assert by_id[str(plain_id)]["outwardTools"] == []
