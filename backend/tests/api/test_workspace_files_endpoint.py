"""GET /agents/{id}/workspace/files: the agent "Files" tab's backend. Lists
every `FileAttachment` (`owner_type="agent_run"`) any of the agent's runs has
produced, across every runtime -- not just the three containerized ones that
used to write a host workspace directory. Content is downloaded through the
shared `GET /files/{attachment_id}` route (see test_files_endpoint.py), not a
route of its own here."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID) -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role="org_admin")


def _h(tenant: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(tenant)}"}


async def test_empty_for_agent_with_no_produced_files(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get(f"/api/v1/agents/{agent_id}/workspace/files", headers=_h(tenant))
            assert r.status_code == 200, r.text
            assert r.json()["files"] == []


async def test_lists_files_across_all_of_the_agents_runs_newest_first(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

        other_agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="B")
        db.add(other_agent)
        await db.flush()

        older_run = m.AgentRun(tenant_id=tenant, agent_id=agent_id, state="done")
        newer_run = m.AgentRun(tenant_id=tenant, agent_id=agent_id, state="done")
        other_agent_run = m.AgentRun(tenant_id=tenant, agent_id=other_agent.id, state="done")
        db.add_all([older_run, newer_run, other_agent_run])
        await db.flush()

        older_file = m.FileAttachment(
            tenant_id=tenant,
            owner_type="agent_run",
            owner_id=older_run.id,
            bucket_key="workspace-files-k1",
            filename="older.txt",
            content_type="text/plain",
            size_bytes=5,
            # created_at is a transaction-scoped `now()` default -- both rows
            # inserted in this one flush would otherwise tie, and the
            # ordering query's id-desc tiebreak isn't reliable within the
            # same millisecond. Set explicit, clearly-ordered timestamps.
            created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        newer_file = m.FileAttachment(
            tenant_id=tenant,
            owner_type="agent_run",
            owner_id=newer_run.id,
            bucket_key="workspace-files-k2",
            filename="newer.txt",
            content_type="text/plain",
            size_bytes=7,
            created_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        )
        # Belongs to a different agent -- must never appear in this agent's list.
        other_file = m.FileAttachment(
            tenant_id=tenant,
            owner_type="agent_run",
            owner_id=other_agent_run.id,
            bucket_key="workspace-files-k3",
            filename="not-mine.txt",
            content_type="text/plain",
            size_bytes=9,
        )
        db.add_all([older_file, newer_file, other_file])
        await db.flush()
        newer_run_id = newer_run.id
        newer_file_id = newer_file.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get(f"/api/v1/agents/{agent_id}/workspace/files", headers=_h(tenant))
            assert r.status_code == 200, r.text
            files = r.json()["files"]
            names = {f["filename"] for f in files}
            assert names == {"older.txt", "newer.txt"}
            # Newest first.
            assert files[0]["filename"] == "newer.txt"
            newest = files[0]
            assert newest["id"] == str(newer_file_id)
            assert newest["runId"] == str(newer_run_id)
            assert newest["sizeBytes"] == 7
            assert newest["contentType"] == "text/plain"


async def test_404_for_missing_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get(
                f"/api/v1/agents/{uuid.uuid4()}/workspace/files", headers=_h(tenant)
            )
            assert r.status_code == 404
