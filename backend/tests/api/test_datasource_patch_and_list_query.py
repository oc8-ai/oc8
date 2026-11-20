"""PATCH /knowledge/sources/{id} and the paged search/filter/group list on GET
/knowledge/sources (Design System Consistency plan, Task 4).

GET /knowledge/sources/{id} DELETE stays untouched and out of scope here: it is
the documented, irreversible operator-delete path (`tombstone_source`) and
there is no restore-the-row endpoint for a DataSource, on purpose.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.knowledge.connectors import registry
from oc8.knowledge.connectors.base import ValidationResult
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _AcceptingConnector:
    """A PATCH with `config` now re-validates the merged config, so the merge
    test needs a connector that is registered and says yes."""

    type_id = "test_patch_merge"
    requires_oauth: str | None = None
    label = "Stub"
    description = "test connector"
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def validate(self, config: dict[str, Any], auth: Any = None) -> ValidationResult:
        return ValidationResult(ok=True)


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


def _client(app: object) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _source(
    db: object,
    tenant: uuid.UUID,
    *,
    name: str = "src",
    connector_type: str = "website",
    classification: str = "internal",
    deleted: bool = False,
) -> m.DataSource:
    ds = m.DataSource(
        tenant_id=tenant,
        connector_type=connector_type,
        name=name,
        config={},
        classification=classification,
        connected=True,
    )
    if deleted:
        import datetime as dt

        ds.deleted_at = dt.datetime.now(tz=dt.UTC)
        ds.connected = False
    db.add(ds)
    await db.flush()
    return ds


# ------------------------------------------------------------------- PATCH


async def test_patch_source_updates_name_and_classification(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        ds = await _source(db, tenant, name="Old Name")
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/sources/{source_id}",
                json={"name": "Renamed Source", "classification": "confidential"},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "Renamed Source"
            assert body["sensitivity"] == "confidential"

    async with app_session(tenant) as db:
        reloaded = await db.get(m.DataSource, source_id)
        assert reloaded is not None
        assert reloaded.name == "Renamed Source"
        assert reloaded.classification == "confidential"


async def test_patch_source_config_is_merged_not_replaced(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source's `config` (e.g. a credential-typed field) must be MERGED, not
    replaced wholesale -- an operator changing which credential a source uses
    must never accidentally wipe its bucket/prefix/other config in the same
    call just because the client didn't resend them."""
    monkeypatch.setitem(registry._CONNECTORS, _AcceptingConnector.type_id, _AcceptingConnector())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        ds = m.DataSource(
            tenant_id=tenant,
            connector_type=_AcceptingConnector.type_id,
            name="src",
            config={"bucket": "reports", "prefix": "2026/", "credential": "old-cred-id"},
            classification="internal",
            connected=True,
        )
        db.add(ds)
        await db.flush()
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/sources/{source_id}",
                json={"config": {"credential": "new-cred-id"}},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            assert r.json()["config"] == {
                "bucket": "reports",
                "prefix": "2026/",
                "credential": "new-cred-id",
            }

    async with app_session(tenant) as db:
        reloaded = await db.get(m.DataSource, source_id)
        assert reloaded is not None
        assert reloaded.config == {
            "bucket": "reports",
            "prefix": "2026/",
            "credential": "new-cred-id",
        }


async def test_patch_source_partial_update_leaves_other_fields_untouched(
    app_session: AppSessionFactory,
) -> None:
    """Omitted fields must not be clobbered -- each is applied only when present
    on the body, same tri-state-adjacent shape as the rest of this codebase's
    PATCH routes."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        ds = await _source(db, tenant, name="Keep Name", classification="internal")
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/sources/{source_id}",
                json={"scheduleCron": "0 * * * *"},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "Keep Name"
            assert body["sensitivity"] == "internal"

    async with app_session(tenant) as db:
        reloaded = await db.get(m.DataSource, source_id)
        assert reloaded is not None
        assert reloaded.schedule_cron == "0 * * * *"
        assert reloaded.name == "Keep Name"


async def test_patch_source_not_found_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/sources/{uuid.uuid4()}",
                json={"name": "x"},
                headers=_headers(tenant),
            )
            assert r.status_code == 404


async def test_patch_source_cross_tenant_404(app_session: AppSessionFactory) -> None:
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    async with app_session(owner) as db:
        ds = await _source(db, owner)
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/sources/{source_id}",
                json={"name": "hijacked"},
                headers=_headers(stranger),
            )
            assert r.status_code == 404

    async with app_session(owner) as db:
        reloaded = await db.get(m.DataSource, source_id)
        assert reloaded is not None
        assert reloaded.name != "hijacked"


async def test_patch_source_requires_knowledge_manage(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        ds = await _source(db, tenant)
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            for role in ("auditor", "operator", "dept_manager"):
                r = await c.patch(
                    f"/api/v1/knowledge/sources/{source_id}",
                    json={"name": "nope"},
                    headers=_headers(tenant, role),
                )
                assert r.status_code == 403, role


# --------------------------------------------------------------- list-query


async def test_list_sources_returns_paged_envelope_and_excludes_deleted(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        live = await _source(db, tenant, name="Live Source")
        gone = await _source(db, tenant, name="Gone Source", deleted=True)
        live_id, gone_id = live.id, gone.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/sources", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert "totalCount" in body
            assert "items" in body
            ids = [s["id"] for s in body["items"]]
            assert str(live_id) in ids
            assert str(gone_id) not in ids
            assert body["totalCount"] == len(body["items"])


async def test_list_sources_include_archived_shows_deleted(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        gone = await _source(db, tenant, name="Gone Source", deleted=True)
        gone_id = gone.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get(
                "/api/v1/knowledge/sources?includeArchived=true", headers=_headers(tenant)
            )
            assert r.status_code == 200, r.text
            ids = [s["id"] for s in r.json()["items"]]
            assert str(gone_id) in ids


async def test_list_sources_search_matches_name(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _source(db, tenant, name="Acme Website")
        await _source(db, tenant, name="Other Drive")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/sources?search=acme", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            names = [s["name"] for s in r.json()["items"]]
            assert names == ["Acme Website"]


async def test_list_sources_filters_by_connector_type(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _source(db, tenant, name="A", connector_type="website")
        await _source(db, tenant, name="B", connector_type="upload")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get(
                "/api/v1/knowledge/sources?connectorType=upload", headers=_headers(tenant)
            )
            assert r.status_code == 200, r.text
            items = r.json()["items"]
            assert [s["name"] for s in items] == ["B"]
            assert all(s["connectorType"] == "upload" for s in items)


async def test_list_sources_group_by_connector_type(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _source(db, tenant, name="B", connector_type="website")
        await _source(db, tenant, name="A", connector_type="upload")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get(
                "/api/v1/knowledge/sources?group_by=connectorType", headers=_headers(tenant)
            )
            assert r.status_code == 200, r.text
            kinds = [s["connectorType"] for s in r.json()["items"]]
            # Grouped by connector_type first: both 'upload' rows (there is
            # exactly one) sort ahead of 'website' alphabetically here, which is
            # enough to prove the group_by took effect rather than falling back
            # to creation order.
            assert kinds == sorted(kinds)


async def test_list_sources_pagination(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        for i in range(5):
            await _source(db, tenant, name=f"S{i}")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/sources?limit=2&offset=1", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["items"]) == 2
            assert body["totalCount"] == 5


async def test_list_sources_cross_tenant_isolation(app_session: AppSessionFactory) -> None:
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    async with app_session(owner) as db:
        await _source(db, owner, name="Owner Source")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/sources", headers=_headers(stranger))
            assert r.status_code == 200, r.text
            assert r.json()["items"] == []
            assert r.json()["totalCount"] == 0
