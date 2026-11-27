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


_VERSION = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _index(dsn: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chat_session_temporary'"
        ).fetchone()
    return row[0] if row else None


def _valid(dsn: str) -> bool | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'ix_chat_session_temporary'"
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


def test_0148_builds_its_index_concurrently() -> None:
    """`chat_session` is written on every turn: a plain CREATE INDEX would
    block those writes for the whole build, as 0144/0146 avoid."""
    source = (_VERSION / "0148_chat_session_temporary.py").read_text()
    assert "SET LOCAL lock_timeout" in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "autocommit_block()" in source


def test_0148_replaces_an_invalid_leftover_of_a_failed_build(pg_url: str) -> None:
    """A CONCURRENTLY build that failed leaves an INVALID index behind, which
    `IF NOT EXISTS` alone would keep for good."""
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE pg_index SET indisvalid = false "
            "WHERE indexrelid = 'ix_chat_session_temporary'::regclass"
        )
    assert _valid(dsn) is False
    command.stamp(cfg, "0147")
    command.upgrade(cfg, "head")
    assert _valid(dsn) is True
