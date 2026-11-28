"""Task 8 of the Design System Consistency plan: `GET /capas/available`
widened to the uniform search/filter/group/pagination contract (spec §1.1) --
`Page[DiscoveredPluginDTO]` instead of a bare list.

This endpoint is unlike Tasks 3-7: it builds its result by scanning plugin
folders on disk (`discover_plugins()`), not by querying the database, so it
cannot use Task 1's `apply_search`/`apply_group_order`/`paginate` SQL helpers.
The filter/sort/slice happens on the already-built Python list instead, but
the wire contract (`{items, totalCount}`) must still match every other
list-query endpoint.

Conventions follow `tests/api/test_capa_discovery_api.py`: no shared
`client`/`tenant_headers` fixtures exist in this suite, so each test builds
its own `AsyncClient` against a fresh `create_app()` and an `org_admin`
bearer token, and points `OC8_CAPAS_PATH` at an isolated `tmp_path` full of
hand-written manifests rather than the repo's real `capas/` folder -- so
these tests do not depend on, or drift with, what plugins happen to ship.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.capas.discovery import MANIFEST_FILENAME
from oc8.config import get_settings
from oc8.main import create_app

pytestmark = pytest.mark.asyncio


def _manifest(name: str, plugin_type: str, summary: str) -> str:
    return f"""
[plugin]
name = "{name}"
version = "1.0.0"
type = "{plugin_type}"
trust = "first_party"
summary = "{summary}"
"""


@pytest.fixture
def plugins_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Three discoverable plugins: two connectors (one named after Odoo) and
    one skill, so search/type/group_by/pagination all have something real to
    filter, sort, and slice."""
    manifests = {
        "acme_odoo_crm": ("connector", "Sync leads from Odoo CRM"),
        "acme_billing": ("connector", "Invoice sync"),
        "acme_helper": ("skill", "A helper skill"),
    }
    for plugin_name, (plugin_type, summary) in manifests.items():
        d = tmp_path / plugin_name
        d.mkdir(parents=True)
        (d / MANIFEST_FILENAME).write_text(_manifest(plugin_name, plugin_type, summary))
    monkeypatch.setenv("OC8_CAPAS_PATH", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _h(tenant: uuid.UUID) -> dict[str, str]:
    tok = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {tok}"}


async def test_list_available_returns_paged_envelope(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/api/v1/capas/available", headers=_h(tenant))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "totalCount" in body
            assert isinstance(body["items"], list)
            assert body["totalCount"] == 3


async def test_list_available_search_matches_name(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(
                "/api/v1/capas/available?search=odoo", headers=_h(tenant)
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            names = [row["name"].lower() for row in body["items"]]
            assert names == ["acme_odoo_crm"]
            assert body["totalCount"] == 1


async def test_list_available_type_filter(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(
                "/api/v1/capas/available?type=skill", headers=_h(tenant)
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["totalCount"] == 1
            assert body["items"][0]["name"] == "acme_helper"


async def test_list_available_group_by_type_sorts_by_type_then_name(
    plugins_root: Path,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(
                "/api/v1/capas/available?group_by=type", headers=_h(tenant)
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            names = [row["name"] for row in body["items"]]
            # connector < skill alphabetically; within connector, sorted by name.
            assert names == ["acme_billing", "acme_odoo_crm", "acme_helper"]


async def test_list_available_unknown_group_by_400s(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(
                "/api/v1/capas/available?group_by=bogus", headers=_h(tenant)
            )
            assert resp.status_code == 400, resp.text


async def test_list_available_pagination(plugins_root: Path) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(
                "/api/v1/capas/available?limit=1&offset=1", headers=_h(tenant)
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["totalCount"] == 3
            assert len(body["items"]) == 1
            # Default sort is by name: acme_billing, acme_helper, acme_odoo_crm.
            assert body["items"][0]["name"] == "acme_helper"


async def test_list_available_bounds_limit_like_its_siblings(plugins_root: Path) -> None:
    """`limit` is bounded here the same way it is on every other list route.

    Task 22 finding: this endpoint took `limit: int = 50` unannotated, so
    `limit=0` answered 200-with-nothing and an arbitrarily large `limit` was
    accepted, while `/skills`, `/agents`, `/departments` and `/knowledge/*`
    all answered 422 for the same input. Same contract, same refusal.
    """
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (
                await c.get("/api/v1/capas/available?limit=0", headers=_h(tenant))
            ).status_code == 422
            assert (
                await c.get("/api/v1/capas/available?limit=100000", headers=_h(tenant))
            ).status_code == 422
            assert (
                await c.get("/api/v1/capas/available?limit=1001", headers=_h(tenant))
            ).status_code == 422
            assert (
                await c.get("/api/v1/capas/available?offset=-1", headers=_h(tenant))
            ).status_code == 422
            # The bound is a ceiling, not a narrowing: the real page still works.
            # The Capas page asks for the whole catalog in one page.
            ok = await c.get("/api/v1/capas/available?limit=1000", headers=_h(tenant))
            assert ok.status_code == 200, ok.text
            assert ok.json()["totalCount"] == 3
