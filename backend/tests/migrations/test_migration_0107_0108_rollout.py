"""0107/0108 build their big indexes CONCURRENTLY and survive a down/up cycle."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

_INDEXES = ("ix_agent_run_agent_created", "ix_chat_message_reply_to_id")
_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _index_state(pg_url: str) -> dict[str, bool]:
    with psycopg.connect(pg_url.replace("+psycopg", "")) as conn:
        rows = conn.execute(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(%s)",
            (list(_INDEXES),),
        ).fetchall()
    return {name: valid for name, valid in rows}


@pytest.mark.parametrize("name", ["0107_chat_c1.py", "0108_chat_live.py"])
def test_the_migration_fails_fast_and_builds_indexes_concurrently(name: str) -> None:
    source = (_VERSIONS / name).read_text()
    assert "SET LOCAL lock_timeout" in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "autocommit_block()" in source


def test_both_indexes_exist_and_are_valid(pg_url: str) -> None:
    assert _index_state(pg_url) == {name: True for name in _INDEXES}


def test_a_downgrade_and_upgrade_cycle_restores_the_schema(pg_url: str) -> None:
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.downgrade(cfg, "0106")
    try:
        assert _index_state(pg_url) == {}
    finally:
        command.upgrade(cfg, "head")
    assert _index_state(pg_url) == {name: True for name in _INDEXES}
