from __future__ import annotations

import httpx
import pytest

from oc8.config import Settings
from oc8.modelrouter import EmbeddingUnavailable
from oc8.modelrouter.router import ModelRouter

pytestmark = pytest.mark.asyncio


async def test_embed_returns_vector_from_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(self, url, json=None, **kwargs):  # type: ignore[no-untyped-def]
        assert url.endswith("/api/embeddings")
        assert json["model"] == "nomic-embed-text"
        assert json["prompt"] == "hello"
        request = httpx.Request("POST", url, json=json)
        return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    router = ModelRouter(Settings(ollama_base_url="http://fake:11434"))
    vec = await router.embed("hello")
    assert vec == [0.1, 0.2, 0.3]


async def test_embed_raises_embedding_unavailable_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_post(self, url, json=None, **kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    router = ModelRouter(Settings(ollama_base_url="http://fake:11434"))
    with pytest.raises(EmbeddingUnavailable):
        await router.embed("hello")


async def test_embed_strips_the_local_namespace_before_calling_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "local/bge-large" is oc8's own UI label for "on-prem" -- Ollama has no
    such namespace and 404s on the literal string. A knowledge base created
    with that choice must actually reach Ollama asking for "bge-large"."""

    async def fake_post(self, url, json=None, **kwargs):  # type: ignore[no-untyped-def]
        assert json["model"] == "bge-large"
        request = httpx.Request("POST", url, json=json)
        return httpx.Response(200, json={"embedding": [0.1, 0.2]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    router = ModelRouter(Settings(ollama_base_url="http://fake:11434"))
    vec = await router.embed("hello", model="local/bge-large")
    assert vec == [0.1, 0.2]


async def test_embed_never_sends_a_cloud_provider_model_to_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `<provider>/<model>` embedding model is routed to that provider for a
    tenant (tests/modelrouter/test_embed_cloud.py). Without a tenant to
    resolve its key it must fail with a message naming the model, never be
    sent to Ollama as a model name Ollama was never going to recognize."""

    def unexpected_post(self, url, json=None, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("must not call Ollama for an unsupported cloud model")

    monkeypatch.setattr(httpx.AsyncClient, "post", unexpected_post)
    router = ModelRouter(Settings(ollama_base_url="http://fake:11434"))
    with pytest.raises(EmbeddingUnavailable, match="openai/text-embedding-3-large"):
        await router.embed("hello", model="openai/text-embedding-3-large")


async def test_embed_with_no_model_override_uses_the_deployment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_post(self, url, json=None, **kwargs):  # type: ignore[no-untyped-def]
        assert json["model"] == "nomic-embed-text"
        request = httpx.Request("POST", url, json=json)
        return httpx.Response(200, json={"embedding": [0.1]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    router = ModelRouter(Settings(ollama_base_url="http://fake:11434"))
    await router.embed("hello")
