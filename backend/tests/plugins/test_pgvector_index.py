"""Unit tests for the external pgvector vector-index capa."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from oc8.knowledge.vector_indexes.base import VectorIndexError

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas" / "pgvector_index"


class _FakeAuth:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def credential(self, credential_id: str, field_key: str) -> str:
        del credential_id
        return self._values[field_key]


def _evict() -> None:
    for name in [n for n in sys.modules if n == "vector_index" or n.startswith("vector_index.")]:
        del sys.modules[name]
    for name in [n for n in sys.modules if n == "credential" or n.startswith("credential.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _plugin_package_path() -> Iterator[None]:
    _evict()
    sys.path.insert(0, str(PLUGINS_DIR))
    yield
    if str(PLUGINS_DIR) in sys.path:
        sys.path.remove(str(PLUGINS_DIR))
    _evict()


def _index() -> Any:
    from vector_index.index import PgvectorVectorIndex  # type: ignore[import-not-found]

    return PgvectorVectorIndex()


async def test_rejects_unsafe_identifiers() -> None:
    index = _index()
    with pytest.raises(VectorIndexError, match="invalid table"):
        await index.search(
            {"table": "evil;drop"},
            _FakeAuth(
                {
                    "host": "h",
                    "database": "d",
                    "user": "u",
                    "password": "p",
                    "port": "5432",
                    "sslmode": "disable",
                }
            ),
            credential_id="c",
            query_embedding=[0.1],
            query_text="q",
            limit=1,
        )


async def test_search_hits_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import vector_index.index as vi  # type: ignore[import-not-found]

    index = _index()

    row = {
        "content": "employee handbook section 3",
        "distance": 0.1,
        "source_uri": "doc://1",
    }
    fetch = AsyncMock(return_value=[row])
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn = MagicMock()
    conn.fetch = fetch
    conn.transaction = MagicMock(return_value=txn)
    conn.close = AsyncMock()

    async def fake_connect(**kwargs: Any) -> MagicMock:
        del kwargs
        return conn

    monkeypatch.setattr(vi.asyncpg, "connect", fake_connect)

    hits = await index.search(
        {
            "table": "docs",
            "embeddingColumn": "embedding",
            "contentColumn": "content",
            "uriColumn": "source_uri",
        },
        _FakeAuth(
            {
                "host": "localhost",
                "database": "ext",
                "user": "u",
                "password": "p",
                "port": "5432",
                "sslmode": "disable",
            }
        ),
        credential_id="c",
        query_embedding=[0.1, 0.2],
        query_text="handbook",
        limit=5,
    )
    assert len(hits) == 1
    assert hits[0].content == "employee handbook section 3"
    assert hits[0].source_uri == "doc://1"
    sql = fetch.await_args.args[0]
    assert "ORDER BY" in sql
    assert '"embedding" <=> $1::vector' in sql
