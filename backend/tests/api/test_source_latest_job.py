"""A source's newest sync job, whatever its state, travels on the source list.

The page showed "queued" from the moment a sync was started and never
again, unless the one button that started it also polled the job -- the
wizard and "Add source" did not, and leaving the page lost it everywhere.
The list now carries the newest job's id and status, so the page reads
the state from the server and keeps asking while a sync is in flight."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _list(tenant: uuid.UUID) -> dict[str, dict[str, object]]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                "/api/v1/knowledge/sources", headers={"Authorization": f"Bearer {token}"}
            )
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


async def test_the_newest_job_is_listed_even_while_it_is_queued(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    now = dt.datetime.now(tz=dt.UTC)
    async with app_session(tenant) as db:
        site = m.DataSource(
            tenant_id=tenant, connector_type="website", name="site", config={}, connected=True
        )
        fresh = m.DataSource(
            tenant_id=tenant, connector_type="website", name="fresh", config={}, connected=True
        )
        db.add_all([site, fresh])
        await db.flush()
        db.add(
            m.IngestionJob(
                tenant_id=tenant,
                data_source_id=site.id,
                status="succeeded",
                stats={},
                created_at=now - dt.timedelta(hours=1),
            )
        )
        queued = m.IngestionJob(
            tenant_id=tenant, data_source_id=site.id, status="queued", stats={}, created_at=now
        )
        db.add(queued)
        await db.commit()
        site_id, fresh_id, queued_id = site.id, fresh.id, queued.id

    items = await _list(tenant)
    assert items[str(site_id)]["latestJobStatus"] == "queued"
    assert items[str(site_id)]["latestJobId"] == str(queued_id)
    assert items[str(fresh_id)]["latestJobStatus"] is None
    assert items[str(fresh_id)]["latestJobId"] is None
