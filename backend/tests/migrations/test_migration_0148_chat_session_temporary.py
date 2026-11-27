"""0148: `chat_session.temporary`, a chat that is not kept."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _column(dsn: str) -> tuple[str, str, str | None] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'chat_session' AND column_name = 'temporary'"
        ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def _index(dsn: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chat_session_temporary'"
        ).fetchone()
    return row[0] if row else None


def test_0148_upgrade_downgrade(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0147")
    try:
        assert _column(dsn) is None
        assert _index(dsn) is None
    finally:
        command.upgrade(cfg, "head")
    assert _column(dsn) == ("boolean", "NO", "false")
    index = _index(dsn)
    assert index is not None and "WHERE temporary" in index
