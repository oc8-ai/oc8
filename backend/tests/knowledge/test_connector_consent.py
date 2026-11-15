"""A capa whose `knowledge:write` is deferred contributes its connectors only
once that permission is granted. Until then the catalog still lists them, as
not usable, naming the consent that is missing and the capa to grant it on."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from oc8.knowledge.connectors.base import ConnectorError
from oc8.knowledge.connectors.registry import consent_pending_connectors, resolve_connector
from tests.capas.deferred_capa import NAME, TYPE_ID, reset, use_capas_path, write_capa
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers

pytestmark = pytest.mark.asyncio


@pytest.fixture
def capas_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    use_capas_path(tmp_path, monkeypatch)
    yield tmp_path
    reset()


async def test_connector_hidden_until_deferred_permission_granted(
    app_session: AppSessionFactory, capas_root: Path
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    write_capa(
        capas_root,
        permissions=["mcp:connect", "knowledge:write"],
        deferred=["knowledge:write"],
    )
    async with api() as c:
        r = await c.post("/api/v1/capas/install-from-disk", json={"pluginId": NAME}, headers=admin)
        assert r.status_code == 201, r.text
        capa_id = r.json()["pluginId"]

        async with app_session(tenant) as db:
            with pytest.raises(ConnectorError):
                await resolve_connector(db, tenant_id=tenant, type_id=TYPE_ID)
            pending = await consent_pending_connectors(db, tenant_id=tenant)
        connector, pending_capa, permission = pending[TYPE_ID]
        assert connector.type_id == TYPE_ID
        assert str(pending_capa) == capa_id
        assert permission == "knowledge:write"

        listed = (await c.get("/api/v1/knowledge/connectors", headers=admin)).json()
        row = next(x for x in listed if x["typeId"] == TYPE_ID)
        assert row["usable"] is False
        assert row["missing"] == "consent:knowledge:write"
        assert row["capaId"] == capa_id

        g = await c.post(
            f"/api/v1/capas/{capa_id}/grant", json={"permission": "knowledge:write"}, headers=admin
        )
        assert g.status_code == 200, g.text

        async with app_session(tenant) as db:
            assert (await resolve_connector(db, tenant_id=tenant, type_id=TYPE_ID)).type_id == (
                TYPE_ID
            )
            assert await consent_pending_connectors(db, tenant_id=tenant) == {}

        listed = (await c.get("/api/v1/knowledge/connectors", headers=admin)).json()
        row = next(x for x in listed if x["typeId"] == TYPE_ID)
        assert row["usable"] is True
        assert row["missing"] is None
