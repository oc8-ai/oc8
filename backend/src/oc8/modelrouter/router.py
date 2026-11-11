"""Model Router — dispatches a neutral completion request to a provider adapter.

Provider resolution is deliberately forgiving so the local (Ollama) path always
works without keys: unknown providers, or Anthropic without a configured key,
fall back to the local default model.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from oc8.config import Settings, get_settings
from oc8.modelrouter.adapters.ollama import OllamaAdapter
from oc8.modelrouter.registry import build_adapter, canonical_provider
from oc8.modelrouter.streaming import stream_with_fallback
from oc8.modelrouter.trim import overflow_tokens, trim_for_overflow
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ModelAdapter,
)
from oc8.observability import record_model_latency

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def locality_for_provider(provider: str) -> str:
    """'local' if the provider string resolves to the ollama adapter via
    the existing canonical-provider mapping, else 'cloud'. Pure — does not
    consult settings/API keys, unlike ModelRouter.resolve()'s own fallback.
    That's deliberate: it means retrieval-time filtering (the caller of this
    function) is always at least as conservative as the real dispatch will
    be, never less."""
    return "local" if canonical_provider(provider) in (None, "ollama") else "cloud"


#: A 4xx in which the provider names the reasoning parameter: this model, or
#: the server in front of it, takes no level. Matched on the provider's own
#: words, like the temperature retry in adapters/anthropic.py, because no list
#: of model names could keep up.
_REASONING_PARAM = re.compile(r"reasoning|effort|think", re.IGNORECASE)

#: `ModelParams.extra` keys that carry a reasoning setting past the level: the
#: OpenAI-shaped `reasoning_effort`, and Anthropic's `thinking` and its old
#: top-level `effort`. Anthropic's `output_config.effort` is handled apart, since
#: the rest of an `output_config` (a response format) must survive.
_REASONING_EXTRA_KEYS = frozenset({"reasoning_effort", "effort", "thinking"})


def _without_reasoning_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """`extra` without any key that sets a reasoning level or thinking."""
    plain = {k: v for k, v in extra.items() if k not in _REASONING_EXTRA_KEYS}
    output_config = plain.get("output_config")
    if isinstance(output_config, dict) and "effort" in output_config:
        rest = {k: v for k, v in output_config.items() if k != "effort"}
        if rest:
            plain["output_config"] = rest
        else:
            del plain["output_config"]
    return plain


#: Only a malformed or unprocessable request can be a refused parameter. Auth,
#: billing, missing-model, size and rate-limit failures are never this.
_PARAM_REFUSAL_STATUSES = frozenset({400, 422})


def rejects_reasoning(exc: Exception) -> bool:
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code not in _PARAM_REFUSAL_STATUSES:
        return False
    # The provider's own words, not the URL the message also carries.
    try:
        text = exc.response.text
    except Exception:
        text = str(exc).split(": ", 1)[-1]
    return bool(_REASONING_PARAM.search(text))


def embedding_local_tag(model: str) -> str | None:
    """The Ollama tag `model` names, or None if it names a cloud provider.

    A bare tag, `local/<tag>` and `ollama/<tag>` (or any alias of the ollama
    provider) are local. Everything else is `<provider>/<model>` and leaves
    the deployment -- the same line chat's restricted-content guard draws
    (canonical provider other than ollama)."""
    namespace, sep, tag = model.partition("/")
    if not sep:
        return model
    if canonical_provider(namespace) == "ollama":
        return tag
    return None


class EmbeddingUnavailable(RuntimeError):
    """Raised when no vector can be produced: the embedding backend is
    unreachable or errors, or a cloud provider is unknown, not enabled for the
    tenant, has no key, or has no embeddings endpoint."""


class EmbeddingMustStayLocal(EmbeddingUnavailable):
    """The content may only reach a local model, and the embedding model is a
    cloud one: the caller can say why a knowledge base gave nothing."""


class ClassificationViolation(RuntimeError):
    """Raised by ModelRouter.complete() when a request carrying
    restricted-classified content would dispatch to a non-local provider
    (tech-spec §9.4 policy interception)."""


class TenantKeyRequired(RuntimeError):
    """Strict BYOK mode: a cloud completion was requested but the tenant has no
    key configured, and require_tenant_model_key is on."""


logger = logging.getLogger(__name__)


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, provider: str, model: str, api_key: str | None = None) -> tuple[str, str]:
        """Return the (canonical_provider, model) actually used, applying fallback.
        A per-tenant `api_key` counts toward availability, so a tenant with a key
        uses the cloud provider even when the platform env key is empty."""
        canonical = canonical_provider(provider) or "ollama"
        if canonical == "anthropic" and not (api_key or self._settings.anthropic_api_key):
            return "ollama", self._settings.default_model
        if canonical == "openai" and not (api_key or self._settings.openai_api_key):
            return "ollama", self._settings.default_model
        if canonical == "ollama":
            return "ollama", model or self._settings.default_model
        return canonical, model

    def _adapter(
        self, canonical: str, *, base_url: str | None = None, api_key: str | None = None
    ) -> ModelAdapter:
        return build_adapter(canonical, self._settings, base_url, api_key)

    def _guard_and_resolve(
        self, req: CompletionRequest
    ) -> tuple[ModelAdapter, CompletionRequest, str, str]:
        """Apply the pre-dispatch guards, then resolve adapter and request.

        Shared by complete() and stream() on purpose: a streaming path that
        re-implemented these would eventually drift, and both guards are
        security-relevant -- asking for a stream must never become a way around
        strict BYOK or the classification refusal.
        """
        # Strict BYOK: refuse a cloud request with no tenant key BEFORE the
        # availability downgrade can silently route it to ollama or the platform
        # key. Checked on the REQUESTED provider, not the resolved one.
        requested = canonical_provider(req.provider) or "ollama"
        if self._settings.require_tenant_model_key and requested != "ollama" and not req.api_key:
            raise TenantKeyRequired(
                f"provider '{requested}' requires a per-tenant API key "
                f"(credential type '{requested}_api_key'); none is configured for this tenant"
            )

        canonical, model = self.resolve(req.provider, req.model, req.api_key)
        if req.contains_restricted and canonical != "ollama":
            raise ClassificationViolation(
                "request contains restricted-classified content; cannot dispatch to "
                f"cloud provider '{canonical}'"
            )
        adapter = self._adapter(canonical, base_url=req.base_url, api_key=req.api_key)
        resolved = CompletionRequest(
            provider=canonical,
            model=model,
            messages=req.messages,
            tools=req.tools,
            params=req.params,
            tenant_id=req.tenant_id,
            agent_id=req.agent_id,
            request_id=req.request_id,
        )
        return adapter, resolved, canonical, model

    @staticmethod
    def _retry_trimmed(req: CompletionRequest, exc: Exception) -> CompletionRequest | None:
        """The same request with enough of the middle dropped to fit, or None.

        Only for one failure: a conversation past the model's context window,
        which the provider reports as a negative max_tokens carrying the exact
        deficit. Retrying anything else would paper over real errors, and
        retrying THIS without trimming would send an identical request.
        """
        over = overflow_tokens(str(exc))
        if over is None:
            return None
        trimmed = trim_for_overflow(req.messages, over_by=over, headroom=req.params.max_tokens)
        if trimmed is None:
            return None
        logger.warning(
            "conversation was %d tokens past the context window; retrying with %d of %d messages",
            over,
            len(trimmed),
            len(req.messages),
        )
        return replace(req, messages=trimmed)

    @staticmethod
    def _retry_without_reasoning(
        req: CompletionRequest, exc: Exception
    ) -> CompletionRequest | None:
        """The same request without the level and the effort, or None.

        Only when the request carried one and the provider's 4xx names it
        (spec 2026-11-11 §9). The member still gets an answer, and the step
        says it came without thinking.
        """
        extra = req.params.extra or {}
        plain_extra = _without_reasoning_extra(extra)
        if req.params.reasoning is None and req.params.effort is None and plain_extra == extra:
            return None
        if not rejects_reasoning(exc):
            return None
        logger.warning(
            "%s/%s refused the reasoning level; retrying once without it",
            req.provider,
            req.model,
        )
        return replace(
            req,
            params=replace(
                req.params,
                reasoning=None,
                effort=None,
                extra=plain_extra if req.params.extra is not None else None,
            ),
        )

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        adapter, resolved, canonical, model = self._guard_and_resolve(req)
        started = time.monotonic()
        try:
            try:
                return await adapter.complete(resolved)
            except Exception as exc:
                retry = self._retry_trimmed(resolved, exc)
                if retry is not None:
                    return await adapter.complete(retry)
                plain = self._retry_without_reasoning(resolved, exc)
                if plain is None:
                    raise
                result = await adapter.complete(plain)
                result.reasoning_dropped = True
                return result
        finally:
            record_model_latency(
                provider=canonical, model=model, ms=(time.monotonic() - started) * 1000
            )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        """Stream a completion, through the same guards as complete().

        Latency is recorded when the stream finishes, so the number means
        "time to full answer" exactly as it does for a non-streamed call.
        """
        adapter, resolved, canonical, model = self._guard_and_resolve(req)
        started = time.monotonic()
        try:
            sent = False
            try:
                async for chunk in stream_with_fallback(adapter, resolved):
                    sent = True
                    # Stamped here, not left to the adapter: `canonical`/`model`
                    # are what resolve() actually decided (a BYOK-missing-key
                    # downgrade included), the same source complete()'s result
                    # implicitly carries via the adapter's own `provider=self.
                    # provider`. A caller reconstructing a CompletionResult from
                    # chunks (oc8.modelrouter.accumulate) needs this to bill and
                    # cache against the model that actually ran, not the one
                    # requested.
                    chunk.provider = canonical
                    chunk.model = model
                    yield chunk
            except Exception as exc:
                # Only before anything has gone out. Once a chunk is on the wire
                # it cannot be unsent, and restarting would splice two different
                # answers together.
                if sent:
                    raise
                follow_up = self._retry_trimmed(resolved, exc)
                dropped = False
                if follow_up is None:
                    follow_up = self._retry_without_reasoning(resolved, exc)
                    dropped = follow_up is not None
                if follow_up is None:
                    raise
                async for chunk in stream_with_fallback(adapter, follow_up):
                    chunk.provider = canonical
                    chunk.model = model
                    if dropped:
                        # On the retry's own first chunk, never a marker of its
                        # own: an empty chunk ahead of a retry that then fails
                        # would read as "already emitted" and block fallback.
                        chunk.reasoning_dropped = True
                        dropped = False
                    yield chunk
        finally:
            record_model_latency(
                provider=canonical, model=model, ms=(time.monotonic() - started) * 1000
            )

    async def embed(
        self,
        text: str,
        model: str | None = None,
        *,
        tenant_id: uuid.UUID | None = None,
        db: AsyncSession | None = None,
        require_local: str | None = None,
    ) -> list[float]:
        """Embed `text` with `model` (a knowledge base's own `embedding_model`,
        or the deployment default).

        - A bare tag (`nomic-embed-text`), `local/<tag>` or `ollama/<tag>` goes
          to Ollama. `local/` is oc8's own UI label for "on-prem"; Ollama has no
          such namespace, so it is stripped before the tag reaches it.
        - `<provider>/<model>` goes to that provider, but only after the same
          checks a chat completion passes before content leaves for it
          (`cloud_embedding_refusal`). The model part may itself contain `/`.

        `require_local` is the caller's reason this content may only be
        embedded by a local model (a `restricted` label, a local-only base, an
        agent on a local model). With it set, any cloud provider is refused --
        the embedding counterpart of chat's ClassificationViolation.

        Anything that keeps the vector from being produced raises
        `EmbeddingUnavailable` naming the provider, the model and the reason,
        so callers that degrade (ingest stores the chunk without a vector,
        retrieval skips the base) can say why.

        Cloud token usage is recorded per tenant, the same `token_usage_record`
        row a completion writes, so embeddings count toward the tenant's and
        the provider's monthly budgets.
        """
        resolved = model or self._settings.default_embedding_model
        tag = embedding_local_tag(resolved)
        if tag is not None:
            return await self._embed_ollama(text, tag)
        provider, _, cloud_model = resolved.partition("/")
        canonical, api_key, base_url = await self._admit_cloud_embedding(
            provider=provider,
            label=resolved,
            tenant_id=tenant_id,
            db=db,
            require_local=require_local,
        )
        assert db is not None and tenant_id is not None  # _admit_cloud_embedding checked
        adapter = self._adapter(canonical, base_url=base_url, api_key=api_key)
        embed_with_usage = getattr(adapter, "embed_with_usage", None)
        if embed_with_usage is None:
            raise EmbeddingUnavailable(
                f"embedding provider {canonical!r} (model {resolved!r}) cannot embed: "
                "its adapter has no embeddings endpoint"
            )
        try:
            vector, tokens = await embed_with_usage(text, cloud_model)
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingUnavailable(
                f"embedding provider {canonical!r} (model {resolved!r}) failed: {exc}"
            ) from exc
        await self._record_embedding_usage(
            db, tenant_id=tenant_id, provider=canonical, model=cloud_model, tokens=tokens
        )
        return vector

    async def cloud_embedding_refusal(
        self,
        model: str,
        *,
        tenant_id: uuid.UUID | None,
        db: AsyncSession | None,
        require_local: str | None = None,
    ) -> str | None:
        """Why `model` may not embed this tenant's content, or None if it may.
        Local models are always allowed. Makes no call to the provider."""
        if embedding_local_tag(model) is not None:
            return None
        try:
            await self._admit_cloud_embedding(
                provider=model.partition("/")[0],
                label=model,
                tenant_id=tenant_id,
                db=db,
                require_local=require_local,
            )
        except EmbeddingUnavailable as exc:
            return str(exc)
        return None

    async def _admit_cloud_embedding(
        self,
        *,
        provider: str,
        label: str,
        tenant_id: uuid.UUID | None,
        db: AsyncSession | None,
        require_local: str | None,
    ) -> tuple[str, str | None, str | None]:
        """Every check chat applies before content reaches a cloud provider,
        in the same order: data that must stay local, tenant entitlement to
        the provider, strict BYOK, a usable key, the provider's monthly limit
        and the tenant's hard budget. Returns (canonical, api_key, base_url)."""
        from oc8.metering.budget import check_budget
        from oc8.metering.provider_budget import check_provider_budget
        from oc8.modelrouter.keys import resolve_model_base_url, resolve_model_key
        from oc8.modelrouter.registry import resolve_provider

        if require_local:
            raise EmbeddingMustStayLocal(
                f"embedding model {label!r} uses cloud provider {provider!r}, but this "
                f"content must stay on a local model: {require_local}"
            )
        if tenant_id is None or db is None:
            raise EmbeddingUnavailable(
                f"embedding model {label!r} uses provider {provider!r}, whose key is "
                "resolved per tenant, but no tenant was given"
            )
        entry = await resolve_provider(db, tenant_id=tenant_id, name=provider)
        if entry is None:
            raise EmbeddingUnavailable(
                f"embedding provider {provider!r} (model {label!r}) is unknown or not "
                "enabled for this tenant"
            )
        canonical = entry.canonical
        api_key = await resolve_model_key(db, tenant_id=tenant_id, provider=canonical)
        if self._settings.require_tenant_model_key and not api_key:
            raise EmbeddingUnavailable(
                f"embedding provider {canonical!r} (model {label!r}) requires a per-tenant "
                f"API key (credential type '{canonical}_api_key'); none is configured"
            )
        if not entry.available(self._settings, api_key):
            raise EmbeddingUnavailable(
                f"embedding provider {canonical!r} (model {label!r}) has no API key "
                f"configured for this tenant (credential type '{canonical}_api_key')"
            )
        provider_budget = await check_provider_budget(db, tenant_id=tenant_id, provider=canonical)
        if provider_budget.exhausted:
            raise EmbeddingUnavailable(
                f"embedding provider {canonical!r} (model {label!r}) has used up its "
                f"monthly token limit for this tenant ({provider_budget.limit})"
            )
        if (await check_budget(db, tenant_id=tenant_id, department_id=None)).hard_exceeded:
            raise EmbeddingUnavailable(
                f"embedding provider {canonical!r} (model {label!r}) refused: the tenant's "
                "hard token budget is exceeded"
            )
        base_url = await resolve_model_base_url(db, tenant_id=tenant_id, provider=canonical)
        return canonical, api_key, base_url

    async def _embed_ollama(self, text: str, model: str) -> list[float]:
        adapter = OllamaAdapter(self._settings.ollama_base_url)
        try:
            return await adapter.embed(text, model)
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(str(exc)) from exc

    @staticmethod
    async def _record_embedding_usage(
        db: AsyncSession, *, tenant_id: uuid.UUID, provider: str, model: str, tokens: int
    ) -> None:
        """One usage row per cloud embedding call, inside a SAVEPOINT so a
        failed metering write can never abort the caller's transaction (an
        ingest or an agent run). Logged, not raised: losing a usage row is
        better than losing the document."""
        from oc8.metering.usage import record_usage

        try:
            async with db.begin_nested():
                await record_usage(
                    db,
                    tenant_id=tenant_id,
                    request_id=uuid.uuid4(),
                    model=model,
                    provider=provider,
                    tokens_in=tokens,
                    tokens_out=0,
                )
        except Exception:
            logger.warning(
                "could not record embedding usage for provider %s model %s",
                provider,
                model,
                exc_info=True,
            )


def get_model_router() -> ModelRouter:
    return ModelRouter(get_settings())
