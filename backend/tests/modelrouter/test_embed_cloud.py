"""Embeddings through a cloud model provider (`<provider>/<model>`).

The provider is resolved for the tenant exactly as a chat completion is: the
tenant must be entitled to it, and the key and base URL come from the tenant's
own bound credential. Anything that keeps a vector from being produced is an
`EmbeddingUnavailable` that names the provider and the model.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.config import Settings
from oc8.credentials.service import create_credential
from oc8.modelrouter import EmbeddingUnavailable
from oc8.modelrouter.adapters.openai_compatible import OpenAICompatibleAdapter
from oc8.modelrouter.registry import ProviderEntry
from oc8.modelrouter.router import ModelRouter
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


class _Recorder:
    """Stands in for `httpx.AsyncClient.post` and remembers each call."""

    def __init__(self, vector: list[float], *, status: int = 200, tokens: int = 7) -> None:
        self.calls: list[dict[str, Any]] = []
        self._vector = vector
        self._status = status
        self._tokens = tokens

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
        async def post(client: Any, url: str, json: Any = None, **kwargs: Any) -> Any:
            return await self.respond(url, json, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        return self

    async def respond(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"url": url, "json": json, "headers": kwargs.get("headers") or {}})
        request = httpx.Request("POST", url, json=json)
        if self._status != 200:
            return httpx.Response(self._status, json={"error": "nope"}, request=request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": self._vector}],
                "model": json["model"],
                "usage": {"prompt_tokens": self._tokens, "total_tokens": self._tokens},
            },
            request=request,
        )


# ------------------------------------------------------------------ adapter


async def test_openai_compatible_adapter_posts_to_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder([0.5, 0.25, 0.125], tokens=3).install(monkeypatch)
    adapter = OpenAICompatibleAdapter("https://llm.example/v1/", "sk-test")

    vector, tokens = await adapter.embed_with_usage("hello", "nomic-embed-text")

    assert vector == [0.5, 0.25, 0.125]
    assert tokens == 3
    [call] = rec.calls
    assert call["url"] == "https://llm.example/v1/embeddings"
    assert call["json"] == {"model": "nomic-embed-text", "input": "hello"}
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert await adapter.embed("hello", "nomic-embed-text") == [0.5, 0.25, 0.125]


async def test_openai_compatible_adapter_raises_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Recorder([], status=401).install(monkeypatch)
    adapter = OpenAICompatibleAdapter("https://llm.example/v1", "bad")
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.embed("hello", "nomic-embed-text")


async def test_openai_compatible_adapter_refuses_an_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Recorder([]).install(monkeypatch)
    adapter = OpenAICompatibleAdapter("https://llm.example/v1", "k")
    with pytest.raises(ValueError, match="no embedding"):
        await adapter.embed("hello", "nomic-embed-text")


# ------------------------------------------------------------------- router


async def _tenant_with_compatible_key(
    db: Any, tenant: uuid.UUID, *, base_url: str = "https://tenant-llm.example/v1"
) -> None:
    await create_credential(
        db,
        tenant_id=tenant,
        name="Tenant LLM",
        credential_type="openai_compatible_api_key",
        field_values={"api_key": "sk-tenant", "base_url": base_url},
    )


async def test_embed_uses_the_tenants_key_and_base_url(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder([0.1, 0.2], tokens=11).install(monkeypatch)
    router = ModelRouter(Settings(mistral_api_key=""))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _tenant_with_compatible_key(db, tenant)
        vec = await router.embed(
            "hello", model="openai_compatible/nomic-embed-text", tenant_id=tenant, db=db
        )
        assert vec == [0.1, 0.2]
        [call] = rec.calls
        assert call["url"] == "https://tenant-llm.example/v1/embeddings"
        assert call["json"] == {"model": "nomic-embed-text", "input": "hello"}
        assert call["headers"]["Authorization"] == "Bearer sk-tenant"

        # Counted like a completion: one usage row for the tenant and provider.
        rows = (
            (
                await db.execute(
                    select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)
                )
            )
            .scalars()
            .all()
        )
        assert [(r.provider, r.model, r.tokens_in, r.tokens_out) for r in rows] == [
            ("openai_compatible", "nomic-embed-text", 11, 0)
        ]


async def test_embed_resolves_a_plugin_provider_for_the_tenant(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capa-contributed OpenAI-compatible provider: the model part is passed
    through verbatim, the plugin's default URL applies when the tenant's
    credential has none."""
    rec = _Recorder([0.3], tokens=2).install(monkeypatch)
    entry = ProviderEntry(
        canonical="acme_ai",
        aliases=("acme",),
        locality="cloud",
        factory=lambda _s, b, key: OpenAICompatibleAdapter(
            b or "https://acme.example/v1", key or ""
        ),
        available=lambda _s, key: bool(key),
    )
    seen: dict[str, Any] = {}

    async def fake_resolve_provider(db: Any, *, tenant_id: uuid.UUID, name: str) -> Any:
        seen["tenant_id"], seen["name"] = tenant_id, name
        return entry

    async def fake_key(db: Any, *, tenant_id: uuid.UUID, provider: str, **_: Any) -> str:
        assert provider == "acme_ai"
        return "sk-acme"

    async def fake_base_url(db: Any, *, tenant_id: uuid.UUID, provider: str, **_: Any) -> None:
        return None

    monkeypatch.setattr("oc8.modelrouter.registry.resolve_provider", fake_resolve_provider)
    monkeypatch.setattr("oc8.modelrouter.keys.resolve_model_key", fake_key)
    monkeypatch.setattr("oc8.modelrouter.keys.resolve_model_base_url", fake_base_url)
    monkeypatch.setattr(
        "oc8.modelrouter.router.build_adapter",
        lambda canonical, settings, base_url=None, api_key=None: entry.factory(
            settings, base_url, api_key
        ),
    )
    router = ModelRouter(Settings())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        vec = await router.embed("hi", model="acme/text-embed/v2", tenant_id=tenant, db=db)
    assert vec == [0.3]
    assert seen == {"tenant_id": tenant, "name": "acme"}
    [call] = rec.calls
    assert call["url"] == "https://acme.example/v1/embeddings"
    assert call["json"]["model"] == "text-embed/v2"
    assert call["headers"]["Authorization"] == "Bearer sk-acme"


async def test_unknown_provider_raises_with_provider_and_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not call out for an unknown provider")

    monkeypatch.setattr(httpx.AsyncClient, "post", unexpected)
    router = ModelRouter(Settings())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(EmbeddingUnavailable, match=r"'nosuch'.*'nosuch/embed-1'"):
            await router.embed("hi", model="nosuch/embed-1", tenant_id=tenant, db=db)


async def test_missing_key_raises_with_provider_and_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not call out without a key")

    monkeypatch.setattr(httpx.AsyncClient, "post", unexpected)
    router = ModelRouter(Settings(mistral_api_key=""))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(
            EmbeddingUnavailable,
            match=r"'openai_compatible'.*'openai_compatible/nomic-embed-text'.*no API key",
        ):
            await router.embed(
                "hi", model="openai_compatible/nomic-embed-text", tenant_id=tenant, db=db
            )


async def test_adapter_without_embeddings_raises(app_session: AppSessionFactory) -> None:
    router = ModelRouter(Settings(anthropic_api_key="sk-platform"))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(EmbeddingUnavailable, match=r"'anthropic'.*cannot embed"):
            await router.embed("hi", model="anthropic/claude-embed", tenant_id=tenant, db=db)


async def test_provider_http_error_becomes_embedding_unavailable(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Recorder([], status=500).install(monkeypatch)
    router = ModelRouter(Settings())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _tenant_with_compatible_key(db, tenant)
        with pytest.raises(EmbeddingUnavailable, match=r"'openai_compatible'.*failed"):
            await router.embed(
                "hi", model="openai_compatible/nomic-embed-text", tenant_id=tenant, db=db
            )


async def test_cloud_model_without_a_tenant_raises() -> None:
    router = ModelRouter(Settings())
    with pytest.raises(EmbeddingUnavailable, match="no tenant"):
        await router.embed("hi", model="openai_compatible/nomic-embed-text")


async def test_local_only_refuses_a_cloud_provider() -> None:
    router = ModelRouter(Settings())
    with pytest.raises(EmbeddingUnavailable, match="local-only"):
        await router.embed(
            "hi",
            model="openai_compatible/nomic-embed-text",
            tenant_id=uuid.uuid4(),
            local_only=True,
        )


async def test_ollama_namespace_and_local_still_go_to_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[tuple[str, str]] = []

    async def fake_post(self: Any, url: str, json: Any = None, **kwargs: Any) -> Any:
        urls.append((url, json["model"]))
        return httpx.Response(200, json={"embedding": [1.0]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    router = ModelRouter(Settings(ollama_base_url="http://ollama:11434"))
    await router.embed("a", model="local/nomic-embed-text", tenant_id=uuid.uuid4())
    await router.embed("b", model="ollama/nomic-embed-text")
    await router.embed("c", model="nomic-embed-text")
    assert urls == [("http://ollama:11434/api/embeddings", "nomic-embed-text")] * 3
