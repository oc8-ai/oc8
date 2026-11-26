"""0139: `token_usage_record.member_id` and its per-member time index."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

_INDEX = "ix_token_usage_record_tenant_ts"
_DRAFT_INDEX = "ix_token_usage_record_tenant_member_ts"


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _member_column(dsn: str) -> tuple[str, str] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'token_usage_record' AND column_name = 'member_id'"
        ).fetchone()
    return (row[0], row[1]) if row else None


def _index(dsn: str, name: str = _INDEX) -> tuple[str, bool] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT pg_get_indexdef(i.indexrelid), i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = %s",
            (name,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def test_0139_upgrade_downgrade(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0138")
    try:
        assert _member_column(dsn) is None
        assert _index(dsn) is None
    finally:
        command.upgrade(cfg, "head")
    assert _member_column(dsn) == ("uuid", "YES")
    index = _index(dsn)
    assert index is not None
    indexdef, valid = index
    assert valid
    assert "(tenant_id, ts) INCLUDE (member_id, tokens_in, tokens_out)" in indexdef
    assert _index(dsn, _DRAFT_INDEX) is None
