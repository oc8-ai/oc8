"""Hybrid retrieval: full-text search next to vector search on `kb_chunk`.

`search_tsv` is generated from `content` with the language-agnostic `simple`
configuration (tenants mix German and English, and an invoice number must
match as written), and indexed with GIN. `embedding` gets an HNSW index for
cosine distance from pgvector 0.8.0 on, when the column has a fixed
dimension; otherwise that index is skipped with a NOTICE and the search stays
an exact scan, as before. 0.5 to 0.7 can build one, but their HNSW scan
returns at most `hnsw.ef_search` rows ahead of the tenant/base filter, so a
filtered search would lose candidates the exact scan finds; 0.8's iterative
scan (which retrieval turns on) keeps scanning until the filtered LIMIT is
met. A database whose image was upgraded needs `ALTER EXTENSION vector
UPDATE` before this migration to get the index.

Adding a STORED generated column rewrites `kb_chunk` once, under an ACCESS
EXCLUSIVE lock: `lock_timeout` keeps it from queueing the app behind a long
transaction, and `statement_timeout` is lifted for that statement because the
rewrite of a large knowledge base takes minutes. Both indexes are built
CONCURRENTLY outside the migration transaction, likewise without a statement
timeout.

Revision ID: 0137
Revises: 0136
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0137"
down_revision: str | None = "0136"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _build_index_concurrently(name: str, ddl: str) -> None:
    """`ddl` (a `CREATE INDEX CONCURRENTLY IF NOT EXISTS`) outside the
    migration transaction, after dropping an INVALID leftover of a failed
    earlier build that `IF NOT EXISTS` would otherwise keep for good. Same as
    `0111_chat_c1.py`, plus no `statement_timeout` for the build itself (a
    session setting here, outside any transaction, so reset afterwards)."""
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        try:
            _drop_invalid_and_build(name, ddl)
        finally:
            op.execute("RESET statement_timeout")


def _drop_invalid_and_build(name: str, ddl: str) -> None:
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": name},
        )
        .first()
    )
    if invalid is not None:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(ddl)


def _hnsw_version_ok(extversion: str | None) -> bool:
    """pgvector 0.8.0 or later (`extversion`, None without the extension)."""
    if not extversion:
        return False
    parts = tuple(int(p) for p in extversion.split(".")[:2] if p.isdigit())
    return parts >= (0, 8)


def _hnsw_supported() -> bool:
    """pgvector new enough (`_hnsw_version_ok`), and `embedding` declared with
    a dimension (`atttypmod` is -1 for a bare `vector`)."""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT "
                "(SELECT extversion FROM pg_extension WHERE extname = 'vector'), "
                "(SELECT atttypmod > 0 FROM pg_attribute "
                " WHERE attrelid = 'kb_chunk'::regclass AND attname = 'embedding' "
                " AND NOT attisdropped)"
            )
        )
        .first()
    )
    return row is not None and _hnsw_version_ok(row[0]) and bool(row[1])


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = 0")
    op.execute(
        "ALTER TABLE kb_chunk ADD COLUMN IF NOT EXISTS search_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED"
    )
    _build_index_concurrently(
        "ix_kb_chunk_search_tsv",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_kb_chunk_search_tsv "
        "ON kb_chunk USING gin (search_tsv)",
    )
    if _hnsw_supported():
        _build_index_concurrently(
            "ix_kb_chunk_embedding_hnsw",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_kb_chunk_embedding_hnsw "
            "ON kb_chunk USING hnsw (embedding vector_cosine_ops)",
        )
    else:
        op.execute(
            "DO $$ BEGIN RAISE NOTICE 'ix_kb_chunk_embedding_hnsw skipped: pgvector "
            "older than 0.8.0 or kb_chunk.embedding has no fixed dimension'; END $$"
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_kb_chunk_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_kb_chunk_search_tsv")
    op.execute("ALTER TABLE kb_chunk DROP COLUMN IF EXISTS search_tsv")
