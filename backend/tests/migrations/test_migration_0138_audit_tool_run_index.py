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
    for name in _NAMES:
        assert "WHERE (category = 'tool_action'::text)" in indexes[name]
