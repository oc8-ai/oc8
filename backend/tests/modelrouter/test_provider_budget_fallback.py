from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.metering.provider_budget import set_provider_budget
from oc8.metering.usage import record_usage
from oc8.modelrouter.fallback import (
    ProviderBudgetExceeded,
    complete_with_fallback,
    stream_completion_with_fallback,
)
from oc8.modelrouter.streaming import chunk_from_result
from oc8.modelrouter.types import CompletionRequest, CompletionResult, ModelParams, Usage
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _Router:
    def __init__(self) -> None:
        self.providers: list[str] = []

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        self.providers.append(req.provider)
        return CompletionResult(
            text="ok", tool_calls=[], usage=Usage(1, 1), stop_reason="stop",
            provider=req.provider, model=req.model,
        )

    async def stream(self, req: CompletionRequest):
        yield chunk_from_result(await self.complete(req))


async def _exhaust(db, tenant: uuid.UUID) -> None:
    await set_provider_budget(db, tenant_id=tenant, provider="opaas_ai", monthly_token_limit=10)
    await record_usage(
        db, tenant_id=tenant, request_id=uuid.uuid4(), model="opaas-echo", provider="opaas_ai",
        tokens_in=10, tokens_out=0,
    )


def _kwargs(tenant: uuid.UUID, primary):
    return dict(
        tenant_id=tenant, agent_id=uuid.uuid4(), primary=primary,
        no_config_provider="opaas_ai", no_config_model="opaas-echo",
        messages=[], tools=[], params=ModelParams(), request_id=uuid.uuid4(),
        contains_restricted=False,
    )


async def test_no_budget_row_means_no_limit(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    router = _Router()
    async with app_session(tenant) as db:
        result = await complete_with_fallback(db, router, **_kwargs(tenant, None))
    assert result.text == "ok" and router.providers == ["opaas_ai"]


async def test_exhausted_provider_without_fallback_raises(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    router = _Router()
    async with app_session(tenant) as db:
        await _exhaust(db, tenant)
        with pytest.raises(ProviderBudgetExceeded) as raised:
            await complete_with_fallback(db, router, **_kwargs(tenant, None))
    assert raised.value.provider == "opaas_ai"
    assert "opaas_ai monthly token quota used up until" in str(raised.value)
    assert router.providers == []


async def test_exhausted_provider_falls_through_to_the_next_model(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    router = _Router()
    async with app_session(tenant) as db:
        await _exhaust(db, tenant)
        fallback = m.ModelConfig(tenant_id=tenant, provider="ollama", model="llama3", locality="local")
        db.add(fallback)
        await db.flush()
        primary = m.ModelConfig(
            tenant_id=tenant, provider="opaas_ai", model="opaas-echo", fallbacks=[str(fallback.id)]
        )
        db.add(primary)
        await db.flush()
        result = await complete_with_fallback(db, router, **_kwargs(tenant, primary))
    assert result.text == "ok" and router.providers == ["ollama"]


async def test_other_providers_are_not_limited(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    router = _Router()
    async with app_session(tenant) as db:
        await _exhaust(db, tenant)
        kwargs = _kwargs(tenant, None) | {"no_config_provider": "ollama", "no_config_model": "llama3"}
        await complete_with_fallback(db, router, **kwargs)
    assert router.providers == ["ollama"]


async def test_streaming_is_checked_too(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    router = _Router()
    async with app_session(tenant) as db:
        await _exhaust(db, tenant)
        with pytest.raises(ProviderBudgetExceeded):
            async for _ in stream_completion_with_fallback(db, router, **_kwargs(tenant, None)):
                pass
    assert router.providers == []


async def test_streaming_falls_through_to_the_next_model(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    router = _Router()
    async with app_session(tenant) as db:
        await _exhaust(db, tenant)
        fallback = m.ModelConfig(tenant_id=tenant, provider="ollama", model="llama3", locality="local")
        db.add(fallback)
        await db.flush()
        primary = m.ModelConfig(
            tenant_id=tenant, provider="opaas_ai", model="opaas-echo", fallbacks=[str(fallback.id)]
        )
        db.add(primary)
        await db.flush()
        chunks = [c async for c in stream_completion_with_fallback(db, router, **_kwargs(tenant, primary))]
    assert chunks and router.providers == ["ollama"]
