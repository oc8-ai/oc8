"""Credential-type test for pgvector_db: open a connection and SELECT 1."""

from __future__ import annotations

import asyncpg


async def validate_pgvector(values: dict[str, str]) -> None:
    host = values.get("host") or ""
    database = values.get("database") or ""
    user = values.get("user") or ""
    password = values.get("password") or ""
    if not host or not database or not user:
        raise ValueError("host, database, and user are required")
    port = int(values.get("port") or "5432")
    sslmode = values.get("sslmode") or "prefer"
    # asyncpg uses ssl=True/False; map common libpq modes coarsely.
    ssl = sslmode not in ("disable", "allow")
    conn = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        ssl=ssl,
        timeout=10,
    )
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()
