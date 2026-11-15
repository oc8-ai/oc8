"""What a connector tells a sync about what it left out, and why.

The report travels in a ContextVar and not on the connector, because a plugin
connector instance is process-global: one object serves every tenant and every
concurrent sync, so any per-sync state kept on it would leak between them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.knowledge.connectors import registry
from oc8.knowledge.connectors.base import RawDocument, ValidationResult
from oc8.knowledge.connectors.report import report_note, report_skip
from oc8.knowledge.ingest import run_source_sync
from oc8.main import create_app
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

STUB = "test_skip_report"


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _Connector:
    type_id = STUB
    requires_oauth: str | None = None
    label = "Stub"
    description = "test connector"
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def validate(self, config: dict[str, Any], auth: Any = None) -> ValidationResult:
        return ValidationResult(ok=True)

    async def discover(self, config: dict[str, Any], auth: Any = None) -> list[Any]:
        return []

    async def fetch(self, config: dict[str, Any], cursor: Any, auth: Any = None) -> Any:
        skips: list[str] = list(config.get("skips", []))
        notes: list[str] = list(config.get("notes", []))
        for note in notes:
            report_note(note)
        for reason in skips:
            report_skip(reason)
        # Yield control mid-fetch so a concurrent sync interleaves with this one.
        await asyncio.sleep(0)
        uri = f"test://{config.get('name', 'doc')}"
        content = f"body of {uri}"
        yield RawDocument(
            source_uri=uri,
            title=uri,
            content=content,
            content_type="text/plain",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector())


async def _setup(
    db: Any, tenant: uuid.UUID, config: dict[str, Any]
) -> tuple[m.KnowledgeBase, m.DataSource]:
    if await db.get(m.Organization, tenant) is None:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
        await db.flush()
    kb = m.KnowledgeBase(tenant_id=tenant, name="KB", embedding_model="nomic-embed-text")
    db.add(kb)
    ds = m.DataSource(
        tenant_id=tenant, connector_type=STUB, name="src", config=config, connected=True
    )
    db.add(ds)
    await db.flush()
    return kb, ds


async def _sync(
    db: Any, tenant: uuid.UUID, kb: m.KnowledgeBase, ds: m.DataSource
) -> m.IngestionJob:
    return await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)


async def test_sync_records_skips_by_reason(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb, ds = await _setup(db, tenant, {"skips": ["unsupported_type"] * 2 + ["too_large"]})
        job = await _sync(db, tenant, kb, ds)
        assert job.status == "succeeded"
        assert job.stats["skipped_by_reason"] == {"unsupported_type": 2, "too_large": 1}


async def test_sync_without_skip_report_has_no_key(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb, ds = await _setup(db, tenant, {})
        job = await _sync(db, tenant, kb, ds)
        assert job.status == "succeeded"
        assert "skipped_by_reason" not in job.stats
        assert "notes" not in job.stats


async def test_report_outside_sync_is_noop() -> None:
    report_skip("unsupported_type")
    report_note("api_key_recommended")


async def test_concurrent_syncs_keep_separate_reports(app_session: AppSessionFactory) -> None:
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    async with app_session(t1) as db1, app_session(t2) as db2:
        kb1, ds1 = await _setup(db1, t1, {"name": "a", "skips": ["too_large"] * 3})
        kb2, ds2 = await _setup(db2, t2, {"name": "b", "skips": ["url_document"]})
        j1, j2 = await asyncio.gather(_sync(db1, t1, kb1, ds1), _sync(db2, t2, kb2, ds2))
        assert j1.stats["skipped_by_reason"] == {"too_large": 3}
        assert j2.stats["skipped_by_reason"] == {"url_document": 1}


async def test_sync_records_notes(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb, ds = await _setup(
            db, tenant, {"notes": ["api_key_recommended", "api_key_recommended", "x" * 100]}
        )
        job = await _sync(db, tenant, kb, ds)
        assert job.stats["notes"] == ["api_key_recommended", "x" * 64]


async def test_list_sources_exposes_last_sync_skipped(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb, ds = await _setup(
            db, tenant, {"skips": ["unsupported_type"], "notes": ["api_key_recommended"]}
        )
        plain = m.DataSource(
            tenant_id=tenant, connector_type=STUB, name="plain", config={}, connected=True
        )
        db.add(plain)
        await db.flush()
        ds_id, plain_id = ds.id, plain.id
        db.add(
            m.IngestionJob(
                tenant_id=tenant,
                data_source_id=ds_id,
                kb_id=kb.id,
                status="succeeded",
                stats={"skipped_by_reason": {"too_large": 9}},
                # Older than the sync below: both rows are written in one
                # transaction, so `now()` would tie and the order fall to the id.
                created_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1),
            )
        )
        await db.flush()
        await _sync(db, tenant, kb, ds)

    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                "/api/v1/knowledge/sources", headers={"Authorization": f"Bearer {token}"}
            )
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    assert items[str(ds_id)]["lastSyncSkipped"] == {"unsupported_type": 1}
    assert items[str(ds_id)]["lastSyncNotes"] == ["api_key_recommended"]
    assert items[str(plain_id)]["lastSyncSkipped"] is None
    assert items[str(plain_id)]["lastSyncNotes"] is None
