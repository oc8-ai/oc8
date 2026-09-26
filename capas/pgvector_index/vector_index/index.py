"""External Postgres/pgvector query-only vector index.

Not oc8's own ``kb_chunk`` table — a customer schema with configurable
table and column names. Read-only: SELECT with ``<=>`` distance only.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

from oc8.knowledge.vector_indexes.base import VectorHit, VectorIndexError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str, *, what: str) -> str:
    if not _IDENT.match(name):
        raise VectorIndexError(f"invalid {what}: {name!r}")
    return name


class PgvectorVectorIndex:
    type_id = "pgvector"
    label = "Postgres / pgvector"
    description = (
        "Search an existing table in an external Postgres database with pgvector "
        "(query-only, read-only session)."
    )
    credential_type = "pgvector_db"
    config_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "title": "Table",
                "description": "Schema-qualified name is not supported; use a single identifier.",
            },
            "embeddingColumn": {
                "type": "string",
                "title": "Embedding column",
                "default": "embedding",
            },
            "contentColumn": {
                "type": "string",
                "title": "Content column",
                "default": "content",
            },
            "uriColumn": {
                "type": "string",
                "title": "Source URI column (optional)",
                "default": "",
            },
            "classificationColumn": {
                "type": "string",
                "title": "Classification column (optional)",
                "default": "",
            },
            "where": {
                "type": "string",
                "title": "Extra WHERE clause (optional)",
                "description": "Static filter only — no placeholders. Leave empty if none.",
                "default": "",
            },
        },
        "required": ["table"],
    }

    async def _connect(self, auth: Any, credential_id: str) -> asyncpg.Connection:
        host = await auth.credential(credential_id, "host")
        database = await auth.credential(credential_id, "database")
        user = await auth.credential(credential_id, "user")
        password = await auth.credential(credential_id, "password")
        try:
            port_raw = await auth.credential(credential_id, "port")
        except Exception:
            port_raw = "5432"
        try:
            sslmode = await auth.credential(credential_id, "sslmode")
        except Exception:
            sslmode = "prefer"
        port = int(port_raw or "5432")
        ssl = (sslmode or "prefer") not in ("disable", "allow")
        try:
            return await asyncpg.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                ssl=ssl,
                timeout=15,
            )
        except Exception as exc:
            raise VectorIndexError(f"could not connect to Postgres: {exc}") from exc

    def _mapping(self, config: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
        table = _ident(str(config.get("table") or "").strip(), what="table")
        emb = _ident(
            str(config.get("embeddingColumn") or "embedding").strip(),
            what="embedding column",
        )
        content = _ident(
            str(config.get("contentColumn") or "content").strip(),
            what="content column",
        )
        uri_raw = str(config.get("uriColumn") or "").strip()
        uri = _ident(uri_raw, what="uri column") if uri_raw else ""
        class_raw = str(config.get("classificationColumn") or "").strip()
        class_col = _ident(class_raw, what="classification column") if class_raw else ""
        where = str(config.get("where") or "").strip()
        if where and (";" in where or "--" in where or "/*" in where):
            raise VectorIndexError("where clause contains disallowed characters")
        return table, emb, content, uri, class_col, where

    async def validate(
        self,
        config: dict[str, Any],
        auth: Any,
        *,
        credential_id: str,
    ) -> None:
        table, emb, content, uri, class_col, _where = self._mapping(config)
        conn = await self._connect(auth, credential_id)
        try:
            # Confirm table + columns exist without selecting data.
            cols = {emb, content}
            if uri:
                cols.add(uri)
            if class_col:
                cols.add(class_col)
            rows = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = $1 AND table_schema = current_schema()
                """,
                table,
            )
            present = {r["column_name"] for r in rows}
            missing = cols - present
            if missing:
                raise VectorIndexError(
                    f"table {table!r} is missing column(s): {', '.join(sorted(missing))}"
                )
        finally:
            await conn.close()

    async def search(
        self,
        config: dict[str, Any],
        auth: Any,
        *,
        credential_id: str,
        query_embedding: list[float],
        query_text: str,
        limit: int,
    ) -> list[VectorHit]:
        del query_text
        table, emb, content, uri, class_col, where = self._mapping(config)
        select_cols = [f'"{content}" AS content', f'"{emb}" <=> $1::vector AS distance']
        if uri:
            select_cols.append(f'"{uri}" AS source_uri')
        if class_col:
            select_cols.append(f'"{class_col}" AS classification')
        sql = f'SELECT {", ".join(select_cols)} FROM "{table}"'
        if where:
            sql += f" WHERE {where}"
        sql += f' ORDER BY "{emb}" <=> $1::vector LIMIT $2'
        # asyncpg wants the vector as a string literal pgvector understands.
        vector_literal = "[" + ",".join(str(float(x)) for x in query_embedding) + "]"
        conn = await self._connect(auth, credential_id)
        try:
            # Read-only transaction: refuse accidental writes even if SQL were wrong.
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(sql, vector_literal, limit)
        except VectorIndexError:
            raise
        except Exception as exc:
            raise VectorIndexError(f"pgvector search failed: {exc}") from exc
        finally:
            await conn.close()

        hits: list[VectorHit] = []
        for i, row in enumerate(rows):
            text = row["content"]
            if not isinstance(text, str) or not text.strip():
                continue
            distance = float(row["distance"] or 0.0)
            score = 1.0 / (1.0 + distance)
            source_uri = row["source_uri"] if uri else f"pgvector://{table}/{i}"
            if not isinstance(source_uri, str) or not source_uri:
                source_uri = f"pgvector://{table}/{i}"
            classification = row["classification"] if class_col else None
            if classification is not None and not isinstance(classification, str):
                classification = str(classification)
            hits.append(
                VectorHit(
                    content=text,
                    source_uri=source_uri,
                    score=score,
                    classification=classification,
                )
            )
        return hits


def register(contrib: Any) -> None:
    contrib.add_vector_index(PgvectorVectorIndex())
