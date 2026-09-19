"""sync_run_output/sync_active_run_outputs: turning a run's
`/workspace/output/` into durable `FileAttachment` rows. Covers the dedup
rule (same filename+size is assumed unchanged and skipped), the
newest-wins rule (a changed size gets a new row), that a rejected file
(oversized/unsupported type) is logged and skipped rather than raised, and
that the periodic sweep only touches RUNNING runs."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.config import get_settings
from oc8.runtime.workspace import output_dir, sync_run_output
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _use_tmp_session_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("OC8_RUNTIME_SESSION_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _dept_agent_run(db: Any, tenant: uuid.UUID) -> m.AgentRun:
    dept = m.Department(tenant_id=tenant, name="Compliance", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add(run)
    await db.flush()
    return run


def _write_output_file(run_id: uuid.UUID, name: str, content: bytes) -> None:
    root = output_dir(run_id)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, name), "wb") as fh:
        fh.write(content)


async def test_uploads_a_new_output_file(app_session: AppSessionFactory, minio_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        _write_output_file(run.id, "report.txt", b"Q3 numbers look good.")

        synced = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        assert [f.filename for f in synced] == ["report.txt"]

        row = (
            await db.execute(
                select(m.FileAttachment).where(m.FileAttachment.owner_id == run.id)
            )
        ).scalar_one()
        assert row.owner_type == "agent_run"
        assert row.tenant_id == tenant
        assert row.size_bytes == len(b"Q3 numbers look good.")
        assert row.extracted_text == "Q3 numbers look good."


async def test_a_second_pass_skips_an_unchanged_file(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        _write_output_file(run.id, "report.txt", b"draft one")

        first = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        assert len(first) == 1

        second = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        assert second == []

        rows = (
            (
                await db.execute(
                    select(m.FileAttachment).where(m.FileAttachment.owner_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_a_changed_size_produces_a_new_row(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        _write_output_file(run.id, "report.txt", b"draft one")
        await sync_run_output(db, tenant_id=tenant, run_id=run.id)

        _write_output_file(run.id, "report.txt", b"the final, longer draft")
        second = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        assert len(second) == 1

        rows = (
            (
                await db.execute(
                    select(m.FileAttachment)
                    .where(m.FileAttachment.owner_id == run.id)
                    .order_by(m.FileAttachment.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[-1].extracted_text == "the final, longer draft"


async def test_an_unsupported_content_type_is_skipped_not_raised(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        # No recognizable extension -- mimetypes.guess_type falls back to
        # application/octet-stream, which is not in ALLOWED_CONTENT_TYPES.
        _write_output_file(run.id, "data.bin", b"\x00\x01\x02")

        synced = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        assert synced == []

        rows = (
            (
                await db.execute(
                    select(m.FileAttachment).where(m.FileAttachment.owner_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


async def test_an_empty_or_missing_output_dir_yields_nothing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _dept_agent_run(db, tenant)
        synced = await sync_run_output(db, tenant_id=tenant, run_id=run.id)
        assert synced == []
