"""0129: a trigger names the login its polled event listens on, and each such
trigger keeps where its poller left off in `event_source_cursor`."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from oc8 import models as m
from oc8.backup.tables import EXCLUDED_TABLES
from tests.conftest import AppSessionFactory

_TRIGGER_COLUMNS = {"event_target_credential_id", "event_config"}
_CURSOR_COLUMNS = {
    "tenant_id",
    "trigger_id",
    "cursor",
    "last_polled_at",
    "last_ok_at",
    "last_error",
    "last_error_at",
    "failures",
    "next_poll_at",
}


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_adds_the_columns_and_the_table_and_downgrade_removes_them(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0128")
    try:
        assert not _TRIGGER_COLUMNS & _columns(dsn, "trigger")
        assert _columns(dsn, "event_source_cursor") == set()
    finally:
        command.upgrade(cfg, "head")
    assert _TRIGGER_COLUMNS <= _columns(dsn, "trigger")
    assert _CURSOR_COLUMNS <= _columns(dsn, "event_source_cursor")


def test_cursors_are_not_backed_up() -> None:
    assert "event_source_cursor" in EXCLUDED_TABLES


async def test_rls_isolates_cursors(app_session: AppSessionFactory) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant_a) as db:
        trigger = m.Trigger(
            tenant_id=tenant_a,
            agent_id=uuid.uuid4(),
            kind="event",
            task_text="t",
            event_source="s",
            event_type="e",
        )
        db.add(trigger)
        await db.flush()
        db.add(m.EventSourceCursor(tenant_id=tenant_a, trigger_id=trigger.id))
    async with app_session(tenant_a) as db:
        rows = (await db.execute(select(m.EventSourceCursor))).scalars().all()
        assert len(rows) == 1
        assert rows[0].cursor is None and rows[0].failures == 0
    async with app_session(tenant_b) as db:
        assert (await db.execute(select(m.EventSourceCursor))).scalars().all() == []


async def test_a_trigger_defaults_to_an_empty_event_config(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="webhook",
            task_text="t",
            webhook_token=uuid.uuid4().hex,
        )
        db.add(trigger)
        await db.flush()
        trigger_id = trigger.id
    async with app_session(tenant) as db:
        row = await db.get(m.Trigger, trigger_id)
        assert row is not None
        assert row.event_config == {} and row.event_target_credential_id is None
