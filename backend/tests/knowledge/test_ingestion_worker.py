from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.knowledge.connectors import registry
from oc8.knowledge.worker import get_ingestion_queue, ingest_job, recover_ingestion
from oc8.runtime.queue import RunMessage
from oc8.runtime.worker import run_worker
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _StubConnector:
    type_id = "test_ingest"
    requires_oauth = None
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def validate(self, config: dict[str, Any], auth: Any = None) -> Any:
        from oc8.knowledge.connectors.base import ValidationResult

        return ValidationResult(ok=True)

    async def discover(self, config: dict[str, Any], auth: Any = None) -> list[Any]:
        return []

    async def fetch(self, config: dict[str, Any], cursor: Any, auth: Any = None) -> Any:
        from oc8.knowledge.connectors.base import RawDocument

        yield RawDocument(
            source_uri="test://1", title="t", content="hello world",
            content_type="text/plain", content_hash="h1",
        )


async def _setup(db: Any, tenant: uuid.UUID, *, status: str = "queued") -> tuple[Any, Any]:
    kb = m.KnowledgeBase(
        tenant_id=tenant, name="kb", description="", embedding_model="nomic-embed-text"
    )
    db.add(kb)
    ds = m.DataSource(tenant_id=tenant, connector_type="test_ingest", name="s", config={})
    db.add(ds)
    await db.flush()
    job = m.IngestionJob(tenant_id=tenant, data_source_id=ds.id, kb_id=kb.id, status=status)
    db.add(job)
    await db.flush()
    return job, ds


@pytest.fixture(autouse=True)
def _register_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry._CONNECTORS, "test_ingest", _StubConnector())


async def test_worker_runs_a_queued_job_to_succeeded(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    # redis_url must be requested (not just implicitly available) -- it is what
    # points get_settings().redis_url at the testcontainer before
    # get_ingestion_queue()'s module-level singleton captures it; see
    # `_reset_realtime_bus`'s docstring in conftest.py for the same footgun.
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        job, _ = await _setup(db, tenant)
        job_id = job.id
        await db.commit()
    q = get_ingestion_queue()
    await q.enqueue(run_id=job_id, tenant_id=tenant)
    await run_worker(q, once=True, handler=ingest_job, recovery=recover_ingestion)
    async with app_session(tenant) as db:
        done = await db.get(m.IngestionJob, job_id)
        assert done is not None
        assert done.status in ("succeeded", "partial")
        assert done.stats.get("chunks", 0) >= 1


async def test_handler_no_ops_a_non_queued_job(app_session: AppSessionFactory) -> None:
    # Redelivery of a job already running/terminal must not re-run.
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        job, _ = await _setup(db, tenant, status="succeeded")
        job_id = job.id
        stats_before = dict(job.stats or {})
        await db.commit()
    await ingest_job(RunMessage(run_id=str(job_id), tenant_id=str(tenant),
                                entry_id="1-0", redelivered=False))
    async with app_session(tenant) as db:
        after = await db.get(m.IngestionJob, job_id)
        assert after is not None
        assert after.status == "succeeded"
        assert dict(after.stats or {}) == stats_before  # untouched


async def test_handler_fails_when_data_source_gone(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(
            tenant_id=tenant, name="kb", description="", embedding_model="nomic-embed-text"
        )
        db.add(kb)
        await db.flush()
        job = m.IngestionJob(tenant_id=tenant, data_source_id=uuid.uuid4(),
                             kb_id=kb.id, status="queued")
        db.add(job)
        await db.flush()
        job_id = job.id
        await db.commit()
    await ingest_job(RunMessage(run_id=str(job_id), tenant_id=str(tenant),
                                entry_id="1-0", redelivered=False))
    async with app_session(tenant) as db:
        after = await db.get(m.IngestionJob, job_id)
        assert after is not None
        assert after.status == "failed"


async def test_recover_marks_stuck_running_failed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        job, _ = await _setup(db, tenant, status="running")
        job_id = job.id
        await db.commit()
    await recover_ingestion(RunMessage(run_id=str(job_id), tenant_id=str(tenant),
                                       entry_id="1-0", redelivered=True))
    async with app_session(tenant) as db:
        after = await db.get(m.IngestionJob, job_id)
        assert after is not None
        assert after.status == "failed"


async def test_recover_after_completed_job_is_a_terminal_no_op(
    app_session: AppSessionFactory,
) -> None:
    # Regression for the reclaim race: ingest_job now commits the
    # queued -> running claim in its own transaction before running the
    # (potentially long) sync loop, so a reclaimer that fires after the
    # claim committed must never re-run a job that has since finished.
    # Simulate that ordering by driving ingest_job to completion first,
    # then calling recover_ingestion on the same job id -- it must see the
    # terminal state and no-op rather than re-run run_source_sync.
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        job, _ = await _setup(db, tenant)
        job_id = job.id
        await db.commit()
    msg = RunMessage(run_id=str(job_id), tenant_id=str(tenant), entry_id="1-0",
                     redelivered=False)
    await ingest_job(msg)
    async with app_session(tenant) as db:
        done = await db.get(m.IngestionJob, job_id)
        assert done is not None
        assert done.status in ("succeeded", "partial")
        chunks_after_first_run = done.stats.get("chunks", 0)
        assert chunks_after_first_run >= 1

    # A reclaiming consumer redelivers the (by now finished) entry.
    await recover_ingestion(RunMessage(run_id=str(job_id), tenant_id=str(tenant),
                                       entry_id="1-0", redelivered=True))

    async with app_session(tenant) as db:
        after = await db.get(m.IngestionJob, job_id)
        assert after is not None
        assert after.status == done.status  # unchanged terminal status
        assert after.stats.get("chunks", 0) == chunks_after_first_run  # no double-ingest


async def test_recover_fails_job_stuck_running_between_claim_and_run(
    app_session: AppSessionFactory,
) -> None:
    # Simulates a worker crashing between session 1 (claim commits
    # queued -> running) and session 2 (the sync loop). recover_ingestion
    # must fail the job rather than re-run the sync, and no chunks should
    # have been written since the sync loop never started.
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        job, _ = await _setup(db, tenant, status="running")
        job_id = job.id
        await db.commit()
    await recover_ingestion(RunMessage(run_id=str(job_id), tenant_id=str(tenant),
                                       entry_id="1-0", redelivered=True))
    async with app_session(tenant) as db:
        after = await db.get(m.IngestionJob, job_id)
        assert after is not None
        assert after.status == "failed"
        chunk_count = (
            await db.execute(select(m.KbChunk).where(m.KbChunk.tenant_id == tenant))
        ).scalars().all()
        assert len(chunk_count) == 0  # run_source_sync never ran -- no chunks written


async def test_ingestion_of_an_inactive_organisation_is_dropped(
    app_session: AppSessionFactory,
) -> None:
    from oc8.tenants.activity import set_tenant_activity_gate

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        job, _ = await _setup(db, tenant)
        job_id = job.id
        await db.commit()

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    set_tenant_activity_gate(gate)
    await ingest_job(RunMessage(run_id=str(job_id), tenant_id=str(tenant),
                                entry_id="1-0", redelivered=False))

    async with app_session(tenant) as db:
        after = await db.get(m.IngestionJob, job_id)
        assert after is not None
        assert after.status == "failed"
        assert after.stats.get("error") == "organisation is not active"
        assert after.stats.get("chunks", 0) == 0
