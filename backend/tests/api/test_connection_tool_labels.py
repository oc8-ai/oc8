"""GET /mcp/connections/{name}/tool-labels (run step timeline, Task 9).

Fixtures are test_mcp_endpoint.py's: the same OC8_CAPAS_PATH override and the
same `_plugin_name`/`_connection_key`-stamped row materialise.py writes.
"""

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


async def _odoo_connection(db: AsyncSession, tenant: uuid.UUID) -> None:
    db.add(
        m.McpConnection(
            tenant_id=tenant,
            name="odoo",
            server_url="",
            transport="stdio",
            scopes=[],
            config={"_plugin_name": "odoo_mcp", "_connection_key": "primary"},
            connected=False,
        )
    )
    await db.flush()


async def _get(tenant: uuid.UUID, name: str) -> tuple[int, dict[str, object]]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/api/v1/mcp/connections/{name}/tool-labels", headers=_h(tenant))
            return resp.status_code, (resp.json() if resp.content else {})


async def test_the_catalogue_carries_the_packs_labels_and_their_german(
    app_session: AppSessionFactory,
) -> None:
    assert _PLUGINS_DIR.is_dir(), _PLUGINS_DIR
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _odoo_connection(db, tenant)
        await db.commit()

    code, body = await _get(tenant, "odoo")
    assert code == 200, body

    assert body["connection"] == "odoo"
    labels = {entry["tool"]: entry for entry in body["labels"]}
    assert labels["search_records"]["verb"] == "Looked up"
    assert labels["search_records"]["object"] == "{model_label}"
    assert labels["search_records"]["verbTranslations"]["de"] == "Nachgesehen"
    assert labels["search_records"]["runningTranslations"]["de"]


async def test_the_catalogue_carries_the_model_labels(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _odoo_connection(db, tenant)
        await db.commit()

    code, body = await _get(tenant, "odoo")
    assert code == 200, body
    models = {entry["key"]: entry for entry in body["modelLabels"]}
    assert models["crm.lead"]["label"] == "deals"
    assert models["crm.lead"]["labelTranslations"]["de"] == "Leads"


async def test_the_catalogue_carries_the_read_modify_split_for_the_fallback(
    app_session: AppSessionFactory,
) -> None:
    """Tier two of the resolution order: a tool with no declared label still
    has to become "Read from odoo" rather than `search_records`, and the
    browser can only know which of the two sentences applies from this."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _odoo_connection(db, tenant)
        await db.commit()

    code, body = await _get(tenant, "odoo")
    assert code == 200, body
    assert "search_records" in body["read"]
    assert "create_record" in body["modify"]
    assert "create_record" not in body["read"]


async def test_an_unknown_connection_is_an_empty_catalogue_not_a_404(
    app_session: AppSessionFactory,
) -> None:
    """A timeline of a run whose connection was deleted must still render --
    with fallback labels, not with an error banner."""
    tenant = uuid.uuid4()
    code, body = await _get(tenant, "does-not-exist")
    assert code == 200, body
    assert body["labels"] == []
    assert body["modelLabels"] == []
    assert body["read"] == []


async def test_a_connection_whose_pack_ships_no_labels_is_empty(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.McpConnection(
                tenant_id=tenant,
                name="gitea",
                server_url="",
                transport="stdio",
                scopes=[],
                config={"_plugin_name": "gitea_mcp", "_connection_key": "primary"},
                connected=False,
            )
        )
        await db.flush()
        await db.commit()

    code, body = await _get(tenant, "gitea")
    assert code == 200, body
    assert body["labels"] == []
    # Its right classification still comes through -- that is what makes the
    # fallback better than a raw name for a pack with no labels at all.
    assert body["read"] or body["modify"]


async def test_the_route_is_permission_gated(app_session: AppSessionFactory) -> None:
    """Same gate as reading a connection. `member` is a real, mintable role
    (`BUILTIN_ROLE_PERMISSIONS[MEMBER_ROLE]` in authz/permissions.py) that
    holds only `copilot:use` and no `integration:view` -- unlike `agent`,
    which is not a role name the catalogue defines at all."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _odoo_connection(db, tenant)
        await db.commit()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(
                "/api/v1/mcp/connections/odoo/tool-labels", headers=_h(tenant, role="member")
            )
    assert resp.status_code == 403, resp.text
