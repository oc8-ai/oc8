"""0138: a chat file records whether its text was indexed (`index_status`) and
which file's index it shares (`indexed_from_id`)."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            ("file_attachment",),
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_adds_and_downgrade_removes_the_columns(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0137")
    try:
        assert not {"index_status", "indexed_from_id"} & _columns(dsn)
    finally:
        command.upgrade(cfg, "head")
    assert {"index_status", "indexed_from_id"} <= _columns(dsn)


def test_index_status_is_checked(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO file_attachment (id, tenant_id, owner_type, owner_id, bucket_key, "
                "filename, content_type, size_bytes, is_image, index_status, created_at, "
                "updated_at) VALUES (%s, %s, 'chat_message', %s, %s, 'f.txt', 'text/plain', 1, "
                "false, 'done', now(), now())",
                (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4().hex),
            )
