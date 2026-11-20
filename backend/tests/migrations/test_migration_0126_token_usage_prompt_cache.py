"""0126 adds token_usage_record's prompt-cache split, defaulted to 0."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

_COLUMNS = {"cache_read_tokens", "cache_write_tokens"}


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(dsn: str) -> dict[str, str | None]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name = 'token_usage_record'"
        ).fetchall()
    return {name: default for name, default in rows if name in _COLUMNS}


def test_upgrade_adds_the_cache_columns_and_downgrade_drops_them(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0125")
    try:
        assert _columns(dsn) == {}
    finally:
        command.upgrade(cfg, "head")
    columns = _columns(dsn)
    assert set(columns) == _COLUMNS
    assert all(default == "0" for default in columns.values())
