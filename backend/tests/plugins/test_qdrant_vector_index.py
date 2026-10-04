"""Unit tests for the Qdrant vector-index capa (mocked HTTP)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from oc8.knowledge.vector_indexes.base import VectorIndexError

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas" / "qdrant"


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
    from vector_index.index import QdrantVectorIndex  # type: ignore[import-not-found]

    return QdrantVectorIndex()


async def test_qdrant_search_maps_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import vector_index.index as vi  # type: ignore[import-not-found]

    index = _index()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/points/query" in str(request.url) or "/points/search" in str(request.url)
        return httpx.Response(
            200,
            json={
                "result": {
                    "points": [
                        {
                            "id": 1,
                            "score": 0.91,
                            "payload": {
                                "text": "vacation policy says 30 days",
                                "source": "hr://vacation",
                            },
                        }
                    ]
                }
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(vi.httpx, "AsyncClient", fake_client)

    hits = await index.search(
        {"collection": "hr"},
        _FakeAuth({"url": "http://qdrant.test", "api_key": "k"}),
        credential_id="cred-1",
        query_embedding=[0.1, 0.2],
        query_text="vacation",
        limit=5,
    )
    assert len(hits) == 1
    assert hits[0].content == "vacation policy says 30 days"
    assert hits[0].source_uri == "hr://vacation"
    assert hits[0].score == pytest.approx(0.91)


async def test_qdrant_validate_missing_collection() -> None:
    index = _index()
    with pytest.raises(VectorIndexError, match="collection"):
        await index.validate(
            {},
            _FakeAuth({"url": "http://qdrant.test"}),
            credential_id="cred-1",
        )
