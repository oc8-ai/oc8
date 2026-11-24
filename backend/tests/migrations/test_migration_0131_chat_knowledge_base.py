"""0131: a knowledge base is either shared with the organisation (`kind='shared'`,
every base that existed before) or holds the files of one chat (`kind='chat'`,
`chat_session_id` set)."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_backfills_shared_and_downgrade_removes_the_columns(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    tenant, kb = uuid.uuid4(), uuid.uuid4()
    command.downgrade(cfg, "0130")
    try:
        assert not {"kind", "chat_session_id"} & _columns(dsn, "knowledge_base")
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO knowledge_base (id, tenant_id, name, description, embedding_model, "
                "chunking_config, classification, freshness, source_ids, status, local_only, "
                "index_type, index_config, created_at, updated_at) VALUES "
                "(%s, %s, 'Handbook', '', 'nomic-embed-text', '{}', 'internal', '{}', '[]', "
                "'current', false, 'internal', '{}', now(), now())",
                (kb, tenant),
            )
    finally:
        command.upgrade(cfg, "head")
    assert {"kind", "chat_session_id"} <= _columns(dsn, "knowledge_base")
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT kind, chat_session_id FROM knowledge_base WHERE id = %s", (kb,)
        ).fetchone()
        assert row == ("shared", None)
        assert conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'uq_knowledge_base_chat_session'"
        ).fetchone()
        conn.execute("DELETE FROM knowledge_base WHERE id = %s", (kb,))


def test_kind_is_checked(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO knowledge_base (id, tenant_id, name, description, embedding_model, "
                "chunking_config, classification, freshness, source_ids, status, local_only, "
                "index_type, index_config, kind, created_at, updated_at) VALUES "
                "(%s, %s, 'x', '', 'm', '{}', 'internal', '{}', '[]', 'current', false, "
                "'internal', '{}', 'private', now(), now())",
                (uuid.uuid4(), uuid.uuid4()),
            )
        conn.rollback()


def test_one_live_chat_base_per_session(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    tenant, session = uuid.uuid4(), uuid.uuid4()
    insert = (
        "INSERT INTO knowledge_base (id, tenant_id, name, description, embedding_model, "
        "chunking_config, classification, freshness, source_ids, status, local_only, "
        "index_type, index_config, kind, chat_session_id, deleted_at, created_at, updated_at) "
        "VALUES (%s, %s, 'Chat files', '', 'm', '{}', 'internal', '{}', '[]', 'current', false, "
        "'internal', '{}', 'chat', %s, %s, now(), now())"
    )
    with psycopg.connect(dsn) as conn:
        # A deleted one does not count: a chat can get a fresh base after its
        # old one was dropped.
        conn.execute(insert, (uuid.uuid4(), tenant, session, "2026-01-01T00:00:00Z"))
        conn.execute(insert, (uuid.uuid4(), tenant, session, None))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (uuid.uuid4(), tenant, session, None))
        conn.rollback()
