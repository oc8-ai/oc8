"""0130: `tool_usage_counter` holds the counts of guardrail usage limits."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from oc8 import models as m
from oc8.backup.tables import EXCLUDED_TABLES
from tests.conftest import AppSessionFactory

_COLUMNS = {
    "id",
    "tenant_id",
    "agent_id",
    "tool_key",
    "tools_key",
    "period",
    "window_start",
    "count",
    "created_at",
    "updated_at",
}


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tool_usage_counter'"
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_adds_the_table_and_downgrade_removes_it(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0129")
    try:
        assert _columns(dsn) == set()
    finally:
        command.upgrade(cfg, "head")
    assert _columns(dsn) == _COLUMNS


def test_one_row_per_window_is_unique(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'uq_tool_usage_counter_window'"
        ).fetchone()
    assert row is not None
    assert row[0] == ("UNIQUE (tenant_id, agent_id, tool_key, tools_key, period, window_start)")


def test_counters_are_not_backed_up() -> None:
    assert "tool_usage_counter" in EXCLUDED_TABLES


async def test_rls_isolates_counters(app_session: AppSessionFactory) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant_a) as db:
        db.add(
            m.ToolUsageCounter(
                tenant_id=tenant_a,
                agent_id=uuid.uuid4(),
                tool_key="email",
                tools_key="mail_send",
                period="day",
                window_start=dt.datetime(2026, 11, 22, tzinfo=dt.UTC),
                count=1,
            )
        )
    async with app_session(tenant_a) as db:
        assert len((await db.execute(select(m.ToolUsageCounter))).scalars().all()) == 1
    async with app_session(tenant_b) as db:
        assert (await db.execute(select(m.ToolUsageCounter))).scalars().all() == []
