"""0120: direct_access_grant -- RLS on, one active grant per (session, tool),
and a conversation's grants go with it."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _table_exists(pg_url: str) -> bool:
    with psycopg.connect(pg_url.replace("+psycopg", "")) as conn:
        row = conn.execute("SELECT to_regclass('public.direct_access_grant')").fetchone()
    return row is not None and row[0] is not None


def test_grant_table_has_rls_the_active_index_and_cascading_keys(pg_url: str) -> None:
    command.upgrade(_cfg(), "head")
    with psycopg.connect(pg_url.replace("+psycopg", "")) as conn:
        rls = conn.execute(
            "SELECT relrowsecurity FROM pg_class WHERE relname = 'direct_access_grant'"
        ).fetchone()
        index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_direct_access_grant_active'"
        ).fetchone()
        rules = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT ccu.table_name, rc.delete_rule "
                "FROM information_schema.referential_constraints rc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = rc.constraint_name "
                "JOIN information_schema.table_constraints tc "
                "  ON tc.constraint_name = rc.constraint_name "
                "WHERE tc.table_name = 'direct_access_grant'"
            ).fetchall()
        }
    assert rls == (True,)
    assert index is not None
    assert "UNIQUE" in index[0] and "revoked_at IS NULL" in index[0]
    assert rules == {"chat_session": "CASCADE", "approval_request": "CASCADE"}


def test_downgrade_drops_and_upgrade_restores(pg_url: str) -> None:
    command.upgrade(_cfg(), "head")
    assert _table_exists(pg_url)
    command.downgrade(_cfg(), "0119")
    assert not _table_exists(pg_url)
    command.upgrade(_cfg(), "head")
    assert _table_exists(pg_url)
