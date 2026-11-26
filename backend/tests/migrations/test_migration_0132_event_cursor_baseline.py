"""0132: a polled trigger's cursor keeps the id of its baseline."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

_COLUMNS = {"baseline_id", "last_skipped", "last_skipped_at"}


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'event_source_cursor'"
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_adds_the_columns_and_downgrade_removes_them(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0131")
    try:
        assert not _COLUMNS & _columns(dsn)
    finally:
        command.upgrade(cfg, "head")
    assert _COLUMNS <= _columns(dsn)
