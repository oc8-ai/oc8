# backend/src/oc8/modelrouter/fallback.py
"""Ordered fallback-chain execution over ModelConfig.fallbacks (§9.4)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.modelrouter.keys import resolve_model_base_url, resolve_model_key
from oc8.modelrouter.router import ModelRouter
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ModelParams,
    NeutralMessage,
    NeutralTool,
)
from oc8.observability import get_tracer


class ProviderBudgetExceeded(RuntimeError):
    """The tenant used its monthly token quota for this provider."""

    def __init__(self, provider: str, resets_at: dt.datetime) -> None:
        self.provider = provider
        self.resets_at = resets_at
        super().__init__(f"{provider} monthly token quota used up until {resets_at:%Y-%m-%d}")


async def _budget_blocks(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider: str
) -> ProviderBudgetExceeded | None:
    """The quota error for `provider`, or None while it still has budget.
    Imported lazily: oc8.metering imports the model registry."""
    from oc8.metering.provider_budget import check_provider_budget

    check = await check_provider_budget(db, tenant_id=tenant_id, provider=provider)
    return ProviderBudgetExceeded(check.provider, check.resets_at) if check.exhausted else None


def is_retryable(exc: Exception) -> bool:
    """5xx, 429, or a transport-level failure (timeout/connection error)."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


def limit_reason(exc: Exception) -> str | None:
    """What an operator should read when a turn hit a limit, else None.

    Two failures are limits rather than faults: the tenant's monthly quota for
    the provider is spent, or the provider itself throttles (429, after the
    fallback chain is exhausted). Neither is fixed by reading a stack trace,
    and the two need different answers -- one waits for a date, the other for
    a moment -- so each gets a sentence of its own.
    """
    if isinstance(exc, ProviderBudgetExceeded):
        return (
            f"The monthly token quota for {exc.provider} is used up until {exc.resets_at:%Y-%m-%d}."
        )
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return "The model provider is rate-limiting requests; try again shortly."
    return None


async def complete_with_fallback(
    db: AsyncSession,
    router: ModelRouter,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    primary: m.ModelConfig | None,
    no_config_provider: str,
    no_config_model: str,
    messages: list[NeutralMessage],
    tools: list[NeutralTool],
    params: ModelParams,
    request_id: uuid.UUID,
    contains_restricted: bool,
) -> CompletionResult:
    """Try `primary`, then each ModelConfig in primary.fallbacks in order,
    on a retryable failure. Audits each fallback transition. Re-raises the
    final exception once every attempt is exhausted. When primary is None,
    makes exactly one attempt with (no_config_provider, no_config_model) --
    an agent without an assigned ModelConfig has no fallback chain to draw
    from, identical to today's behavior."""
    with get_tracer().start_as_current_span("model.complete") as span:
        span.set_attribute(
            "provider", no_config_provider if primary is None else primary.provider
        )
        span.set_attribute("model", no_config_model if primary is None else primary.model)
        if primary is None:
            blocked = await _budget_blocks(db, tenant_id=tenant_id, provider=no_config_provider)
            if blocked is not None:
                raise blocked
            api_key = await resolve_model_key(
                db, tenant_id=tenant_id, provider=no_config_provider
            )
            base_url = await resolve_model_base_url(
                db, tenant_id=tenant_id, provider=no_config_provider
            )
            req = CompletionRequest(
                provider=no_config_provider,
                model=no_config_model,
                messages=messages,
                tools=tools,
                params=params,
                tenant_id=tenant_id,
                agent_id=agent_id,
                request_id=request_id,
                contains_restricted=contains_restricted,
                base_url=base_url,
                api_key=api_key,
            )
            return await router.complete(req)

        chain: list[m.ModelConfig] = [primary]
        for fb_id in primary.fallbacks or []:
            fb = await db.get(m.ModelConfig, uuid.UUID(str(fb_id)))
            if fb is not None:
                chain.append(fb)

        for i, config in enumerate(chain):
            is_last = i == len(chain) - 1
            blocked = await _budget_blocks(db, tenant_id=tenant_id, provider=config.provider)
            if blocked is not None:
                if is_last:
                    raise blocked
                next_config = chain[i + 1]
                await append_event(
                    db,
                    tenant_id=tenant_id,
                    actor_type="agent",
                    actor_id=agent_id,
                    category="model_router",
                    action="fallback",
                    resource={
                        "from": f"{config.provider}:{config.model}",
                        "to": f"{next_config.provider}:{next_config.model}",
                        "reason": type(blocked).__name__,
                    },
                    decision="fallback",
                )
                continue
            api_key = await resolve_model_key(
                db,
                tenant_id=tenant_id,
                provider=config.provider,
                credential_id=config.credential_id,
            )
            # A per-model `params.base_url` (never written today, but kept as
            # a future per-ModelConfig override) wins over the bound
            # credential's own base_url -- same precedence order as the
            # `resolve_model_key`/`(config.params or {}).get("base_url")` pair
            # `oc8.modelrouter.discovery` already documents.
            base_url = (config.params or {}).get("base_url") or await resolve_model_base_url(
                db,
                tenant_id=tenant_id,
                provider=config.provider,
                credential_id=config.credential_id,
            )
            req = CompletionRequest(
                provider=config.provider,
                model=config.model,
                messages=messages,
                tools=tools,
                params=params,
                tenant_id=tenant_id,
                agent_id=agent_id,
                request_id=request_id,
                contains_restricted=contains_restricted,
                base_url=base_url,
                api_key=api_key,
            )
            try:
                return await router.complete(req)
            except Exception as exc:
                if is_last or not is_retryable(exc):
                    raise
                next_config = chain[i + 1]
                await append_event(
                    db,
                    tenant_id=tenant_id,
                    actor_type="agent",
                    actor_id=agent_id,
                    category="model_router",
                    action="fallback",
                    resource={
                        "from": f"{config.provider}:{config.model}",
                        "to": f"{next_config.provider}:{next_config.model}",
                        "reason": type(exc).__name__,
                    },
                    decision="fallback",
                )
        raise RuntimeError("unreachable: fallback chain exhausted without raising")


async def stream_completion_with_fallback(
    db: AsyncSession,
    router: ModelRouter,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    primary: m.ModelConfig | None,
    no_config_provider: str,
    no_config_model: str,
    messages: list[NeutralMessage],
    tools: list[NeutralTool],
    params: ModelParams,
    request_id: uuid.UUID,
    contains_restricted: bool,
) -> AsyncIterator[CompletionChunk]:
    """Streaming counterpart of complete_with_fallback (§9.4).

    One rule differs, and it is the important one: a retryable failure is only
    retried while NOTHING has been emitted yet. Once the first chunk is out, the
    bytes are already on their way to the client, and falling over to the next
    provider would append a second, overlapping answer to the same response. So
    after the first chunk the exception propagates and the caller ends the stream.
    """
    with get_tracer().start_as_current_span("model.stream") as span:
        span.set_attribute(
            "provider", no_config_provider if primary is None else primary.provider
        )
        span.set_attribute("model", no_config_model if primary is None else primary.model)

        chain: list[m.ModelConfig] = []
        if primary is not None:
            chain.append(primary)
            for fb_id in primary.fallbacks or []:
                fb = await db.get(m.ModelConfig, uuid.UUID(str(fb_id)))
                if fb is not None:
                    chain.append(fb)

        # No ModelConfig means no chain to draw from -- exactly one attempt, the
        # same as complete_with_fallback.
        attempts: list[tuple[str, str, dict[str, Any] | None, uuid.UUID | None]] = (
            [(no_config_provider, no_config_model, None, None)]
            if not chain
            else [(c.provider, c.model, c.params or {}, c.credential_id) for c in chain]
        )

        for i, (provider, model, cfg_params, credential_id) in enumerate(attempts):
            is_last = i == len(attempts) - 1
            blocked = await _budget_blocks(db, tenant_id=tenant_id, provider=provider)
            if blocked is not None:
                if is_last:
                    raise blocked
                nxt = attempts[i + 1]
                await append_event(
                    db,
                    tenant_id=tenant_id,
                    actor_type="agent",
                    actor_id=agent_id,
                    category="model_router",
                    action="fallback",
                    resource={
                        "from": f"{provider}:{model}",
                        "to": f"{nxt[0]}:{nxt[1]}",
                        "reason": type(blocked).__name__,
                        "streaming": True,
                    },
                )
                continue
            api_key = await resolve_model_key(
                db, tenant_id=tenant_id, provider=provider, credential_id=credential_id
            )
            base_url = (cfg_params or {}).get("base_url") or await resolve_model_base_url(
                db, tenant_id=tenant_id, provider=provider, credential_id=credential_id
            )
            req = CompletionRequest(
                provider=provider,
                model=model,
                messages=messages,
                tools=tools,
                params=params,
                tenant_id=tenant_id,
                agent_id=agent_id,
                request_id=request_id,
                contains_restricted=contains_restricted,
                base_url=base_url,
                api_key=api_key,
            )
            emitted = False
            try:
                async for chunk in router.stream(req):
                    emitted = True
                    yield chunk
                return
            except Exception as exc:
                if emitted or is_last or not is_retryable(exc):
                    raise
                nxt = attempts[i + 1]
                await append_event(
                    db,
                    tenant_id=tenant_id,
                    actor_type="agent",
                    actor_id=agent_id,
                    category="model_router",
                    action="fallback",
                    resource={
                        "from": f"{provider}:{model}",
                        "to": f"{nxt[0]}:{nxt[1]}",
                        "reason": type(exc).__name__,
                        "streaming": True,
                    },
                )
