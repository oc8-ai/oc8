"""The web UI's second gate on record links (ruling R9).

`vet_answer_links` reads Markdown with regexes; the browser reads it with a
real parser. So the chat renderer asks the server itself: which hosts are
connected (`GET /record-links/hosts`), and which of the hrefs it is about to
make clickable oc8 actually issued (`POST /record-links/verify`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.capas.manifest import RecordUrlTemplate, ToolPackConnection
from oc8.main import create_app
from oc8.records.registry import remember_links
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

ISSUED = "https://odoo.example.com/odoo/sale.order/42"


def _h(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _call(tenant: uuid.UUID, method: str, path: str, **kw: Any) -> tuple[int, Any]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.request(method, f"/api/v1/record-links{path}", headers=_h(tenant), **kw)
            return resp.status_code, resp.json()


async def _verify(tenant: uuid.UUID, urls: list[str]) -> list[str]:
    code, body = await _call(tenant, "POST", "/verify", json={"urls": urls})
    assert code == 200, body
    return list(body["issued"])


async def test_hosts_are_the_linked_and_the_connected_ones(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = RecordUrlTemplate(
        template="{base_url}/odoo/{model}/{id}",
        base_url_path=["env", "ODOO_URL"],
        models={"sale.order": "sale.order"},
    )
    conn = ToolPackConnection(key="primary", name="odoo", server_url="", record_url=template)
    monkeypatch.setattr(
        "oc8.capas.discovery.resolve_tool_pack_connection", lambda plugin, key: conn
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb")
        db.add(dept)
        await db.flush()
        db.add(
            m.McpConnection(
                tenant_id=tenant,
                department_id=dept.id,
                name="odoo",
                transport="stdio",
                server_url="stdio://odoo",
                connected=True,
                config={
                    "command": "x",
                    "args": [],
                    "env": {"ODOO_URL": "https://ERP.Example.com."},
                    "_plugin_name": "odoo_mcp",
                    "_connection_key": "primary",
                },
            )
        )
        await remember_links(db, tenant_id=tenant, urls=[ISSUED])

    code, body = await _call(tenant, "GET", "/hosts")
    assert code == 200, body
    assert sorted(body["hosts"]) == ["erp.example.com", "odoo.example.com"]


async def test_verify_returns_the_issued_url_as_submitted(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    bracketed = "https://odoo.example.com/odoo/action-1?ids=[1,2]#Größe"
    async with app_session(tenant) as db:
        await remember_links(db, tenant_id=tenant, urls=[ISSUED, bracketed])

    # The browser re-encodes what the model wrote; the match is canonical.
    reencoded = "https://ODOO.example.com/odoo/action-1?ids=%5B1,2%5D#Gr%C3%B6%C3%9Fe"
    submitted = [ISSUED, reencoded, "https://odoo.example.com/odoo/sale.order/99"]
    assert sorted(await _verify(tenant, submitted)) == sorted([ISSUED, reencoded])


async def test_verify_matches_a_backslash_path_the_browser_reads_as_slashes(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await remember_links(db, tenant_id=tenant, urls=[ISSUED])
    raw = "https://odoo.example.com\\odoo\\sale.order\\42"
    assert await _verify(tenant, [raw]) == [raw]


async def test_verify_does_not_confirm_a_different_port_or_scheme(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await remember_links(db, tenant_id=tenant, urls=[ISSUED])
    others = [
        "https://odoo.example.com:8443/odoo/sale.order/42",
        "http://odoo.example.com/odoo/sale.order/42",
        "https://odoo.example.com/odoo/sale.order/42/../99",
    ]
    assert await _verify(tenant, others) == []
    assert await _verify(tenant, ["https://odoo.example.com:443/odoo/sale.order/42"]) == [
        "https://odoo.example.com:443/odoo/sale.order/42"
    ]


async def test_verify_ignores_another_tenants_links(app_session: AppSessionFactory) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    async with app_session(theirs) as db:
        await remember_links(db, tenant_id=theirs, urls=[ISSUED])
    assert await _verify(mine, [ISSUED]) == []


async def test_verify_ignores_a_link_older_than_the_ttl(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await remember_links(db, tenant_id=tenant, urls=[ISSUED])
        await db.execute(
            update(m.RecordLink)
            .where(m.RecordLink.tenant_id == tenant)
            .values(issued_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=31))
        )
    assert await _verify(tenant, [ISSUED]) == []


async def test_verify_of_nothing_or_of_junk_is_empty(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    assert await _verify(tenant, []) == []
    assert await _verify(tenant, ["not a url", "javascript:alert(1)", "https://[x"]) == []


@pytest.mark.parametrize(
    "urls",
    [
        [f"https://o.example/{i}" for i in range(101)],
        ["https://o.example/" + "a" * 2001],
    ],
)
async def test_verify_refuses_more_than_its_limits(
    app_session: AppSessionFactory, urls: list[str]
) -> None:
    code, _body = await _call(uuid.uuid4(), "POST", "/verify", json={"urls": urls})
    assert code == 422
