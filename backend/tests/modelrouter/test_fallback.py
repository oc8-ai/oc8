from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.modelrouter.fallback import complete_with_fallback, is_retryable
from oc8.modelrouter.types import CompletionResult, ModelParams, NeutralMessage, Usage
from tests.audit.helpers import flush_audit
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def test_is_retryable_on_5xx_and_429() -> None:
    assert is_retryable(_http_error(500)) is True
    assert is_retryable(_http_error(503)) is True
    assert is_retryable(_http_error(429)) is True


def test_is_retryable_false_on_other_4xx() -> None:
    assert is_retryable(_http_error(400)) is False
    assert is_retryable(_http_error(401)) is False


def test_is_retryable_on_transport_errors() -> None:
    request = httpx.Request("POST", "https://example.test")
    assert is_retryable(httpx.TimeoutException("timeout", request=request)) is True
    assert is_retryable(httpx.ConnectError("refused", request=request)) is True


def test_is_retryable_false_on_unrelated_exception() -> None:
    assert is_retryable(ValueError("not an http error")) is False


class _ScriptedRouter:
    """Each entry in `script` is either a CompletionResult (success) or an
    Exception instance (raise it). One entry consumed per complete() call."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[Any] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.calls.append(req)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[no-any-return]


def _ok(provider: str, model: str) -> CompletionResult:
    return CompletionResult(
        text="ok", tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
        stop_reason="stop", provider=provider, model=model,
    )


async def test_no_config_makes_exactly_one_attempt(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        router = _ScriptedRouter([_ok("anthropic", "claude")])
        result = await complete_with_fallback(
            db, router, tenant_id=tenant, agent_id=uuid.uuid4(), primary=None,  # type: ignore[arg-type]
            no_config_provider="anthropic", no_config_model="claude",
            messages=[NeutralMessage(role="user", content="hi")], tools=[],
            params=ModelParams(), request_id=uuid.uuid4(), contains_restricted=False,
        )
        assert result.provider == "anthropic"
        assert len(router.calls) == 1


async def test_primary_succeeds_no_fallback_attempted(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        primary = m.ModelConfig(
            tenant_id=tenant, provider="openai", model="gpt-4o", locality="cloud"
        )
        db.add(primary)
        await db.flush()
        router = _ScriptedRouter([_ok("openai", "gpt-4o")])
        result = await complete_with_fallback(
            db, router, tenant_id=tenant, agent_id=uuid.uuid4(), primary=primary,  # type: ignore[arg-type]
            no_config_provider="", no_config_model="",
            messages=[NeutralMessage(role="user", content="hi")], tools=[],
            params=ModelParams(), request_id=uuid.uuid4(), contains_restricted=False,
        )
        assert result.provider == "openai"
        assert len(router.calls) == 1


async def test_retryable_failure_falls_back_and_audits(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = uuid.uuid4()
    async with app_session(tenant) as db:
        fb = m.ModelConfig(
            tenant_id=tenant, provider="ollama", model="mistral:latest", locality="local"
        )
        db.add(fb)
        await db.flush()
        primary = m.ModelConfig(
            tenant_id=tenant, provider="openai", model="gpt-4o", locality="cloud",
            fallbacks=[str(fb.id)],
        )
        db.add(primary)
        await db.flush()
        router = _ScriptedRouter([_http_error(503), _ok("ollama", "mistral:latest")])
        result = await complete_with_fallback(
            db, router, tenant_id=tenant, agent_id=agent_id, primary=primary,  # type: ignore[arg-type]
            no_config_provider="", no_config_model="",
            messages=[NeutralMessage(role="user", content="hi")], tools=[],
            params=ModelParams(), request_id=uuid.uuid4(), contains_restricted=False,
        )
        assert result.provider == "ollama"
        assert len(router.calls) == 2

        await flush_audit(db)  # staged audit events reach the chain at commit
        events = (
            await db.execute(
                select(m.AuditEvent).where(
                    m.AuditEvent.action == "fallback",
                    m.AuditEvent.actor_id == agent_id,
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].resource["from"] == "openai:gpt-4o"
        assert events[0].resource["to"] == "ollama:mistral:latest"


async def test_non_retryable_failure_does_not_fall_back(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        fb = m.ModelConfig(
            tenant_id=tenant, provider="ollama", model="mistral:latest", locality="local"
        )
        db.add(fb)
        await db.flush()
        primary = m.ModelConfig(
            tenant_id=tenant, provider="openai", model="gpt-4o", locality="cloud",
            fallbacks=[str(fb.id)],
        )
        db.add(primary)
        await db.flush()
        router = _ScriptedRouter([_http_error(400)])
        with pytest.raises(httpx.HTTPStatusError):
            await complete_with_fallback(
                db, router, tenant_id=tenant, agent_id=uuid.uuid4(), primary=primary,  # type: ignore[arg-type]
                no_config_provider="", no_config_model="",
                messages=[NeutralMessage(role="user", content="hi")], tools=[],
                params=ModelParams(), request_id=uuid.uuid4(), contains_restricted=False,
            )
        assert len(router.calls) == 1


async def test_chain_exhausted_propagates_final_exception(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        fb = m.ModelConfig(
            tenant_id=tenant, provider="ollama", model="mistral:latest", locality="local"
        )
        db.add(fb)
        await db.flush()
        primary = m.ModelConfig(
            tenant_id=tenant, provider="openai", model="gpt-4o", locality="cloud",
            fallbacks=[str(fb.id)],
        )
        db.add(primary)
        await db.flush()
        router = _ScriptedRouter([_http_error(503), _http_error(503)])
        with pytest.raises(httpx.HTTPStatusError):
            await complete_with_fallback(
                db, router, tenant_id=tenant, agent_id=uuid.uuid4(), primary=primary,  # type: ignore[arg-type]
                no_config_provider="", no_config_model="",
                messages=[NeutralMessage(role="user", content="hi")], tools=[],
                params=ModelParams(), request_id=uuid.uuid4(), contains_restricted=False,
            )
        assert len(router.calls) == 2
