"""0147: `run_input.web`, a queued message's "Web" switch."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _web_column(dsn: str) -> tuple[str, str, str | None] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'run_input' AND column_name = 'web'"
        ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def test_0147_upgrade_downgrade(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0146")
    try:
        assert _web_column(dsn) is None
    finally:
        command.upgrade(cfg, "head")
    assert _web_column(dsn) == ("boolean", "NO", "false")
