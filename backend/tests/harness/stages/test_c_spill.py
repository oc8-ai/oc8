import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.harness.stages.c_spill import (
    SPILL_HEAD_CHARS,
    SPILL_TAIL_CHARS,
    SPILL_THRESHOLD_CHARS,
    Spill,
    maybe_spill,
    persist_spill,
    spill_filename,
)
from oc8.storage.attachments import AttachmentTooLarge


def test_threshold_and_window_constants() -> None:
    assert SPILL_THRESHOLD_CHARS == 8_000
    assert SPILL_HEAD_CHARS == 3_000
    assert SPILL_TAIL_CHARS == 800


def test_filename_sanitises_the_tool_name() -> None:
    assert spill_filename(3, "search_records") == "step-3-search_records.txt"
    assert spill_filename(12, "odoo/search records") == "step-12-odoo_search_records.txt"


def test_a_short_body_is_returned_untouched() -> None:
    body = "ok"
    text, spill = maybe_spill("search_records", body, step_no=1)
    assert text is body
    assert spill is None


def test_a_body_at_the_threshold_is_untouched() -> None:
    body = "x" * SPILL_THRESHOLD_CHARS
    text, spill = maybe_spill("search_records", body, step_no=1)
    assert text is body
    assert spill is None


def test_an_oversized_body_becomes_a_preview_and_a_spill() -> None:
    middle = SPILL_THRESHOLD_CHARS - SPILL_HEAD_CHARS - SPILL_TAIL_CHARS + 50
    body = "H" * SPILL_HEAD_CHARS + "M" * middle + "T" * SPILL_TAIL_CHARS
    assert len(body) == SPILL_THRESHOLD_CHARS + 50
    text, spill = maybe_spill("search_records", body, step_no=4)
    assert spill is not None
    assert spill.filename == "step-4-search_records.txt"
    assert spill.content == body
    assert text.startswith("[search_records returned ")
    assert f'kept as file "{spill.filename}"' in text
    assert "Use read_run_file(" in text
    assert text.endswith("T" * SPILL_TAIL_CHARS)
    omitted = len(body) - SPILL_HEAD_CHARS - SPILL_TAIL_CHARS
    assert f"… {omitted} chars omitted …" in text
    assert "M" * middle not in text


@pytest.mark.asyncio
async def test_persist_spill_writes_an_agent_run_attachment(app_session, minio_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = m.AgentRun(tenant_id=tenant, agent_id=uuid.uuid4(), state="running")
        db.add(run)
        await db.flush()
        row = await persist_spill(
            db,
            tenant_id=tenant,
            run_id=run.id,
            spill=Spill(filename="step-1-search_records.txt", content="hello-spill"),
        )
        assert row.owner_type == "agent_run"
        assert row.owner_id == run.id
        assert row.filename == "step-1-search_records.txt"
        assert row.content_type == "text/plain"
        assert row.extracted_text == "hello-spill"
        found = (
            await db.execute(
                select(m.FileAttachment).where(
                    m.FileAttachment.filename == "step-1-search_records.txt"
                )
            )
        ).scalar_one()
        assert found.id == row.id


@pytest.mark.asyncio
async def test_persist_spill_logs_and_skips_storage_failures(
    app_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reject(*args, **kwargs):
        raise AttachmentTooLarge("too large")

    monkeypatch.setattr("oc8.agent.harness.stages.c_spill.store_attachment_bytes", reject)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        row = await persist_spill(
            db,
            tenant_id=tenant,
            run_id=uuid.uuid4(),
            spill=Spill(filename="step-1-search_records.txt", content="body"),
        )
    assert row is None
