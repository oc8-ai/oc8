"""0143: `kb_chunk.search_tsv`, a generated full-text column with a GIN index,
and an HNSW index on `kb_chunk.embedding` where pgvector supports one."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config

_PATH = Path(__file__).resolve().parents[2] / "migrations" / "versions"
_PATH = _PATH / "0143_kb_chunk_hybrid_search.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("oc8_migration_0143", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("extversion", "built"),
    [
        ("0.4.4", False),
        ("0.5.1", False),
        ("0.7.4", False),  # HNSW, but no iterative scan: a filtered search loses rows
        ("0.8.0", True),
        ("0.8.1", True),
        ("1.0.0", True),
        (None, False),  # no pgvector extension
    ],
)
def test_the_hnsw_index_needs_pgvector_0_8(extversion: str | None, built: bool) -> None:
    """Before 0.8 an HNSW scan returns at most `hnsw.ef_search` rows ahead of
    the tenant/base filter, so a filtered search silently loses candidates an
    exact scan finds; 0.8 adds the iterative scan retrieval turns on."""
    assert _module()._hnsw_version_ok(extversion) is built


def _cfg() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _state(dsn: str) -> tuple[str | None, set[str], bool]:
    """(generated kind of search_tsv or None, kb_chunk index names, hnsw available)."""
    with psycopg.connect(dsn) as conn:
        generated = conn.execute(
            "SELECT attgenerated FROM pg_attribute "
            "WHERE attrelid = 'kb_chunk'::regclass AND attname = 'search_tsv' "
            "AND NOT attisdropped"
        ).fetchone()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'kb_chunk'"
            ).fetchall()
        }
        version = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    hnsw = version is not None and tuple(int(p) for p in version[0].split(".")[:2]) >= (0, 8)
    return (generated[0] if generated else None), indexes, hnsw


def test_0143_upgrade_downgrade(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    cfg = _cfg()
    command.downgrade(cfg, "0142")
    try:
        generated, indexes, _ = _state(dsn)
        assert generated is None
        assert not {"ix_kb_chunk_search_tsv", "ix_kb_chunk_embedding_hnsw"} & indexes
    finally:
        command.upgrade(cfg, "head")
    generated, indexes, hnsw = _state(dsn)
    assert generated == "s"  # STORED
    assert "ix_kb_chunk_search_tsv" in indexes
    assert ("ix_kb_chunk_embedding_hnsw" in indexes) is hnsw


def test_search_tsv_follows_content(pg_url: str) -> None:
    dsn = pg_url.replace("+psycopg", "")
    chunk_id = uuid.uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO kb_chunk (id, tenant_id, kb_id, content, source_uri, metadata, "
            "classification, created_at, updated_at) VALUES (%s, %s, %s, %s, 'upload://a', "
            "'{}', 'internal', now(), now())",
            (chunk_id, uuid.uuid4(), uuid.uuid4(), "Rechnung RE-2026-004711 bezahlt"),
        )
        matched = conn.execute(
            "SELECT search_tsv @@ websearch_to_tsquery('simple', 'RE-2026-004711') "
            "FROM kb_chunk WHERE id = %s",
            (chunk_id,),
        ).fetchone()
        conn.execute("UPDATE kb_chunk SET content = 'other' WHERE id = %s", (chunk_id,))
        after = conn.execute(
            "SELECT search_tsv @@ websearch_to_tsquery('simple', 'RE-2026-004711') "
            "FROM kb_chunk WHERE id = %s",
            (chunk_id,),
        ).fetchone()
        conn.rollback()
    assert matched == (True,)
    assert after == (False,)
