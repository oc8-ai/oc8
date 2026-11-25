"""0138: partial expression indexes on the run and chat a tool event names."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

_NAMES = {"ix_audit_event_tool_run", "ix_audit_event_tool_chat"}


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


def test_0138_upgrade_downgrade(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0137")
    try:
        assert not _NAMES & set(_indexes(dsn))
    finally:
        command.upgrade(cfg, "head")
    indexes = _indexes(dsn)
    assert _NAMES <= set(indexes)
    assert "(resource ->> 'run_id'::text)" in indexes["ix_audit_event_tool_run"]
    assert "(resource ->> 'chat_session_id'::text)" in indexes["ix_audit_event_tool_chat"]
    # Not partial: `GET /audit` filters every event by run, not only tool events.
    for name in _NAMES:
        assert " WHERE " not in indexes[name]


def _generic_plan(dsn: str, **filters: object) -> str:
    """The plan Postgres picks for `GET /audit`'s own query -- compiled from
    `_filtered`, prepared, and planned GENERICALLY (as a re-used prepared
    statement is), with sequential scans priced out."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import asyncpg

    from oc8 import models as m
    from oc8.api.v1.audit import _filtered

    stmt = _filtered(
        select(m.AuditEvent),
        category=None,
        action=None,
        actor_type=None,
        decision=None,
        responsible_type=None,
        from_ts=None,
        to_ts=None,
        **filters,  # type: ignore[arg-type]
    )
    compiled = stmt.compile(dialect=asyncpg.dialect())
    args = ", ".join(f"'{compiled.params[name]}'" for name in compiled.positiontup or [])
    with psycopg.connect(dsn) as conn:
        conn.execute("SET enable_seqscan = off")
        conn.execute("SET plan_cache_mode = force_generic_plan")
        conn.execute(f"PREPARE audit_q AS {compiled}")
        plan = "\n".join(
            row[0] for row in conn.execute(f"EXPLAIN EXECUTE audit_q({args})").fetchall()
        )
        conn.execute("DEALLOCATE audit_q")
        conn.rollback()
    return plan


def test_the_run_and_chat_filters_use_their_indexes(pg_url: str) -> None:
    import uuid

    dsn = pg_url.replace("+psycopg", "")
    assert "ix_audit_event_tool_run" in _generic_plan(dsn, run_id=uuid.uuid4())
    assert "ix_audit_event_tool_chat" in _generic_plan(dsn, chat_session_id=uuid.uuid4())
