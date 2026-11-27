"""0146: a partial index on (tenant_id, ts) for the `chat.feedback` events."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

_NAME = "ix_audit_event_feedback_tenant_ts"


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _indexes(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn:
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'audit_event'"
            ).fetchall()
        }


def test_0146_upgrade_downgrade(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0145")
    try:
        assert _NAME not in _indexes(dsn)
    finally:
        command.upgrade(cfg, "head")
    definition = _indexes(dsn)[_NAME]
    assert "(tenant_id, ts)" in definition
    assert "WHERE (action = 'chat.feedback'::text)" in definition


def _plan(dsn: str, **bounds: object) -> str:
    """The plan of `GET /stats/feedback`'s own query, compiled from
    `feedback_counts_query` and planned generically (as a re-used prepared
    statement is), with sequential scans priced out."""
    from sqlalchemy.dialects.postgresql import asyncpg

    from oc8.api.v1.kpis import feedback_counts_query

    stmt = feedback_counts_query(
        tenant_id=uuid.uuid4(), agent_id=None, department_id=None, **bounds
    )  # type: ignore[arg-type]
    compiled = stmt.compile(dialect=asyncpg.dialect())
    args = ", ".join(f"'{compiled.params[name]}'" for name in compiled.positiontup or [])
    with psycopg.connect(dsn) as conn:
        conn.execute("SET enable_seqscan = off")
        conn.execute("SET plan_cache_mode = force_generic_plan")
        conn.execute(f"PREPARE feedback_q AS {compiled}")
        plan = "\n".join(
            row[0] for row in conn.execute(f"EXPLAIN EXECUTE feedback_q({args})").fetchall()
        )
        conn.execute("DEALLOCATE feedback_q")
        conn.rollback()
    return plan


def test_the_feedback_count_reads_the_partial_index(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    since = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    assert _NAME in _plan(dsn, date_from=None, date_to=None)
    assert _NAME in _plan(dsn, date_from=since, date_to=since + dt.timedelta(days=30))
