"""End-to-end Auto LLM routing: catalog → agent binding → run → escalate → audit.

The LLM provider boundary is a capturing stub (same pattern as
``tests/coding/test_engine_model_config.py``); everything else — HTTP catalog,
agent model-config assignment, ``run_agent``, auto-router policy, cascade, and
audit — is the real path.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.conftest import AppSessionFactory

from oc8 import config
from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.modelrouter import CompletionResult, Usage, chunk_from_result

pytestmark = pytest.mark.asyncio


@pytest.fixture
def platform_llm_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloud Auto tiers count as connected only with a usable key."""
    settings = config.get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-e2e-anthropic", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-e2e-openai", raising=False)


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


class _CascadeCapturingRouter:
    """First answer is deliberately uncertain so cascade_verify escalates once."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.calls.append((req.provider, req.model))
        if len(self.calls) == 1:
            return CompletionResult(
                text="short",
                tool_calls=[],
                usage=Usage(tokens_in=2, tokens_out=2),
                stop_reason="stop",
                provider=req.provider,
                model=req.model,
            )
        return CompletionResult(
            text=(
                "Here is a clear, confident answer with enough detail to pass "
                "the cascade uncertainty check and finish the run."
            ),
            tool_calls=[],
            usage=Usage(tokens_in=5, tokens_out=20),
            stop_reason="stop",
            provider=req.provider,
            model=req.model,
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def test_auto_router_catalog_agent_run_cascade_and_audit(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    platform_llm_keys: None,
) -> None:
    tenant = uuid.uuid4()

    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        dept_id = str(dept.id)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = _headers(tenant)

            # 1. Providers list exposes virtual Auto entry.
            providers = await client.get("/api/v1/models/providers", headers=headers)
            assert providers.status_code == 200, providers.text
            assert any(p["canonical"] == "auto" for p in providers.json())

            # 2. Concrete tier models.
            fast = await client.post(
                "/api/v1/models",
                json={
                    "provider": "ollama",
                    "model": "tinyllama",
                    "locality": "local",
                    "displayName": "Fast local",
                },
                headers=headers,
            )
            assert fast.status_code == 201, fast.text
            balanced = await client.post(
                "/api/v1/models",
                json={
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "locality": "cloud",
                    "displayName": "Balanced",
                },
                headers=headers,
            )
            assert balanced.status_code == 201, balanced.text
            strong = await client.post(
                "/api/v1/models",
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet",
                    "locality": "cloud",
                    "displayName": "Strong",
                },
                headers=headers,
            )
            assert strong.status_code == 201, strong.text
            fast_id = fast.json()["id"]
            balanced_id = balanced.json()["id"]
            strong_id = strong.json()["id"]

            # 3. Auto ModelConfig with cascade verify.
            auto = await client.post(
                "/api/v1/models",
                json={
                    "provider": "auto",
                    "model": "router",
                    "locality": "cloud",
                    "displayName": "Auto Router",
                    "autoTiers": {
                        "fast": fast_id,
                        "balanced": balanced_id,
                        "strong": strong_id,
                    },
                    "autoCascadeVerify": True,
                },
                headers=headers,
            )
            assert auto.status_code == 201, auto.text
            body = auto.json()
            assert body["provider"] == "auto"
            assert body["autoCascadeVerify"] is True
            assert body["autoTiers"]["fast"] == fast_id
            auto_id = body["id"]

            # 4. Health check on Auto resolves tier targets.
            health = await client.post(f"/api/v1/models/{auto_id}/test", headers=headers)
            assert health.status_code == 200, health.text
            assert health.json()["status"] == "healthy"

            # 5. Hire agent + bind Auto.
            created = await client.post(
                "/api/v1/agents",
                json={
                    "name": "Auto Runner",
                    "departmentId": dept_id,
                    "roleTitle": "Router",
                    "mission": "route",
                },
                headers=headers,
            )
            assert created.status_code == 201, created.text
            agent_id = created.json()["id"]

            bound = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": auto_id},
                headers=headers,
            )
            assert bound.status_code == 200, bound.text
            assert bound.json()["modelConfigId"] == auto_id

    # 6. Real run_agent path: short prompt → fast; uncertain → cascade to balanced.
    router = _CascadeCapturingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, uuid.UUID(agent_id))
        assert agent is not None
        assert str(agent.model_config_id) == auto_id

        result = await run_agent(db, agent=agent, task_text="hi", tenant_id=tenant)
        assert result.status == "done", result.output
        # Preamble + tools push the heuristic above FAST even for a short task
        # text, so the first pick is balanced; cascade_verify then escalates.
        assert router.calls == [
            ("openai", "gpt-4o-mini"),
            ("anthropic", "claude-sonnet"),
        ]

        events = (
            (
                await db.execute(
                    select(m.AuditEvent)
                    .where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "auto_route",
                    )
                    .order_by(m.AuditEvent.seq)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) >= 2
        tiers = [e.resource.get("tier") for e in events]
        reasons = [e.resource.get("reason") for e in events]
        assert "balanced" in tiers
        assert "strong" in tiers
        assert any(str(r).startswith("cascade_verify") for r in reasons)
        assert all(e.category == "model_router" for e in events)
