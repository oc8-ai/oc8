"""0123 admits `scope_changed` as a kb_chunk deleted_reason, and its downgrade
narrows the CHECK again without validating rows that already carry it."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _check(dsn: str) -> tuple[str, bool]:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
            "WHERE conname = 'ck_kb_chunk_deleted_reason'"
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_upgrade_admits_scope_changed_and_downgrade_narrows_unvalidated(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()

    definition, validated = _check(dsn)
    assert "scope_changed" in definition
    assert validated

    command.downgrade(cfg, "0122")
    try:
        definition, validated = _check(dsn)
        assert "scope_changed" not in definition
        assert "superseded" in definition
        assert not validated
    finally:
        command.upgrade(cfg, "head")

    definition, validated = _check(dsn)
    assert "scope_changed" in definition
    assert validated
