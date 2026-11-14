"""0119: the record_link registry (RLS on) and the two approval columns."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def test_record_link_table_has_rls_and_approval_columns(pg_url: str) -> None:
    command.upgrade(_cfg(), "head")
    with psycopg.connect(pg_url.replace("+psycopg", "")) as conn:
        rls = conn.execute(
            "SELECT relrowsecurity FROM pg_class WHERE relname = 'record_link'"
        ).fetchone()
        cols = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT column_name, data_type, column_default FROM information_schema.columns "
                "WHERE table_name = 'approval_request' "
                "AND column_name IN ('related_links', 'result_url')"
            ).fetchall()
        }
    assert rls == (True,)
    assert cols["related_links"][0] == "jsonb"
    assert "'[]'" in (cols["related_links"][1] or "")
    assert cols["result_url"][0] == "text"


def test_downgrade_drops_and_upgrade_restores(pg_url: str) -> None:
    command.downgrade(_cfg(), "0118")
    command.upgrade(_cfg(), "head")
