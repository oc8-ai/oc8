"""Auto LLM routing: tiers, escalation latch, shadow audit, cascade, preference."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import config
from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.modelrouter.auto_router import (
    AUTO_MODEL,
    AUTO_PROVIDER,
    AutoRouterError,
    ComplexityTier,
    agent_affinity,
    answer_looks_uncertain,
    cascade_should_escalate,
    escalate_auto_router,
    estimate_complexity,
    preference_examples_for,
    preference_score,
    record_preference_label,
    remember_agent_route,
    resolve_auto_config,
    store_agent_affinity,
)
from oc8.modelrouter.types import NeutralMessage, NeutralTool
from tests.conftest import AppSessionFactory


@dataclass
class _FakeRun:
    """Stand-in for AgentRun.context latching without FK plumbing."""

    context: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def platform_llm_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloud tiers need a key (tenant BYOK or platform) to count as connected."""
    settings = config.get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-anthropic", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-openai", raising=False)


def test_estimate_complexity_fast_for_short_prompt() -> None:
    msgs = [NeutralMessage(role="user", content="Hi")]
    assert estimate_complexity(messages=msgs) == ComplexityTier.FAST


def test_estimate_complexity_strong_for_tools_and_keywords() -> None:
    msgs = [
        NeutralMessage(
            role="user",
            content="Please analyse and refactor this algorithm step by step " + ("x" * 5000),
        )
    ]
    tools = [NeutralTool(name="run", description="d", parameters={"type": "object"})]
    assert estimate_complexity(messages=msgs, tools=tools) == ComplexityTier.STRONG


def test_answer_looks_uncertain() -> None:
    assert answer_looks_uncertain("short") is True
    assert answer_looks_uncertain("I'm not sure about the right approach here.") is True
    assert (
        answer_looks_uncertain(
            "Here is a clear, confident answer with enough detail to pass the length check."
        )
        is False
    )


def test_preference_score_blends_examples() -> None:
    score = preference_score(
        messages=[NeutralMessage(role="user", content="security compliance audit legal")],
        examples=[
            {"text": "security compliance legal audit", "needs_strong": True},
            {"text": "hello world greeting", "needs_strong": False},
        ],
    )
    assert score is not None and score >= 0.55


async def _seed_tiers(
    db, tenant: uuid.UUID
) -> tuple[m.ModelConfig, m.ModelConfig, m.ModelConfig, m.ModelConfig]:
    fast = m.ModelConfig(
        tenant_id=tenant,
        provider="ollama",
        model="fast",
        locality="local",
        display_name="Fast",
        params={},
    )
    balanced = m.ModelConfig(
        tenant_id=tenant,
        provider="openai",
        model="balanced",
        locality="cloud",
        display_name="Balanced",
        params={},
    )
    strong = m.ModelConfig(
        tenant_id=tenant,
        provider="anthropic",
        model="strong",
        locality="cloud",
        display_name="Strong",
        params={"supports_vision": True},
    )
    db.add_all([fast, balanced, strong])
    await db.flush()
    auto = m.ModelConfig(
        tenant_id=tenant,
        provider=AUTO_PROVIDER,
        model=AUTO_MODEL,
        locality="cloud",
        display_name="Auto",
        params={
            "tiers": {
                "fast": str(fast.id),
                "balanced": str(balanced.id),
                "strong": str(strong.id),
            }
        },
    )
    db.add(auto)
    await db.flush()
    return auto, fast, balanced, strong


@pytest.mark.asyncio
async def test_resolve_picks_fast_and_audits(
    app_session: AppSessionFactory, platform_llm_keys: None
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, fast, _balanced, _strong = await _seed_tiers(db, tenant)
        concrete, decision = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=None,
            messages=[NeutralMessage(role="user", content="hi")],
            tools=[],
        )
        assert concrete.id == fast.id
        assert decision.tier == "fast"
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "auto_route",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert events
        assert events[-1].resource["tier"] == "fast"
        assert events[-1].category == "model_router"


@pytest.mark.asyncio
async def test_session_affinity_and_escalation(
    app_session: AppSessionFactory, platform_llm_keys: None
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, fast, balanced, strong = await _seed_tiers(db, tenant)
        run = _FakeRun()
        concrete, decision = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=run,  # type: ignore[arg-type]
            messages=[NeutralMessage(role="user", content="hi")],
        )
        assert concrete.id == fast.id
        assert decision.tier == "fast"

        concrete2, decision2 = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=run,  # type: ignore[arg-type]
            messages=[NeutralMessage(role="user", content="hi again")],
        )
        assert concrete2.id == fast.id
        assert decision2.reason == "session_affinity"

        bumped = await escalate_auto_router(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=run,  # type: ignore[arg-type]
            reason="tool_error",
            messages=[NeutralMessage(role="user", content="hi")],
        )
        assert bumped is not None
        concrete3, decision3 = bumped
        assert concrete3.id == balanced.id
        assert decision3.escalated is True

        bumped2 = await escalate_auto_router(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=run,  # type: ignore[arg-type]
            reason="cascade_verify",
            messages=[NeutralMessage(role="user", content="hi")],
        )
        assert bumped2 is not None
        assert bumped2[0].id == strong.id


@pytest.mark.asyncio
async def test_shadow_only_logs_would_tier(
    app_session: AppSessionFactory, platform_llm_keys: None
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, _fast, balanced, _strong = await _seed_tiers(db, tenant)
        auto.params = {**(auto.params or {}), "shadow_only": True}
        await db.flush()

        concrete, decision = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=_FakeRun(),  # type: ignore[arg-type]
            messages=[
                NeutralMessage(
                    role="user",
                    content="Please analyse and refactor this algorithm " + ("x" * 5000),
                )
            ],
            tools=[NeutralTool(name="t", description="d", parameters={"type": "object"})],
        )
        assert decision.shadow_would_tier == "strong"
        assert decision.reason == "shadow_only"
        assert concrete.id == balanced.id


@pytest.mark.asyncio
async def test_preference_router_raises_tier(
    app_session: AppSessionFactory, platform_llm_keys: None
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, _fast, _balanced, strong = await _seed_tiers(db, tenant)
        auto.params = {
            **(auto.params or {}),
            "preference_router": True,
            "preference_examples": [
                {"text": "security compliance legal audit", "needs_strong": True},
            ],
        }
        await db.flush()
        concrete, decision = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=None,
            messages=[
                NeutralMessage(role="user", content="security compliance legal audit please")
            ],
        )
        assert concrete.id == strong.id
        assert decision.preference_score is not None
        assert decision.preference_score >= 0.55


@pytest.mark.asyncio
async def test_restricted_forces_local_tier(
    app_session: AppSessionFactory, platform_llm_keys: None
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, fast, _balanced, _strong = await _seed_tiers(db, tenant)
        concrete, _decision = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=None,
            messages=[
                NeutralMessage(
                    role="user",
                    content="Please analyse and refactor this algorithm " + ("x" * 5000),
                )
            ],
            tools=[NeutralTool(name="t", description="d", parameters={"type": "object"})],
            contains_restricted=True,
        )
        assert concrete.id == fast.id
        assert concrete.locality == "local"
        # Capability filter already dropped cloud tiers; reason stays complexity.


@pytest.mark.asyncio
async def test_skips_unconnected_and_unhealthy_cloud_tiers(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cloud tiers without a key, or with health=error, must never be selected."""
    settings = config.get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, fast, balanced, _strong = await _seed_tiers(db, tenant)
        # Explicitly unhealthy even if a key appeared later.
        balanced.health = {"status": "error", "error": "key rejected"}
        # _strong has no platform/tenant key → not connected
        await db.flush()

        concrete, decision = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=None,
            messages=[
                NeutralMessage(
                    role="user",
                    content="Please analyse and refactor this algorithm " + ("x" * 5000),
                )
            ],
            tools=[NeutralTool(name="t", description="d", parameters={"type": "object"})],
        )
        assert concrete.id == fast.id
        assert concrete.provider == "ollama"
        assert decision.tier == "fast"


@pytest.mark.asyncio
async def test_errors_when_no_connected_tiers(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config.get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, _fast, balanced, strong = await _seed_tiers(db, tenant)
        # Only cloud tiers, none connected.
        auto.params = {
            "tiers": {
                "balanced": str(balanced.id),
                "strong": str(strong.id),
            }
        }
        await db.flush()
        with pytest.raises(AutoRouterError, match="no connected models"):
            await resolve_auto_config(
                db,
                auto,
                tenant_id=tenant,
                agent_id=None,
                run=None,
                messages=[NeutralMessage(role="user", content="hi")],
            )


@pytest.mark.asyncio
async def test_cascade_self_check_escalates_when_inadequate(
    app_session: AppSessionFactory,
    platform_llm_keys: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))

    class _InadequateRouter:
        async def complete(self, req: Any) -> Any:
            from oc8.modelrouter import CompletionResult, Usage

            return CompletionResult(
                text='{"adequate": false, "reason": "missing steps"}',
                tool_calls=[],
                usage=Usage(tokens_in=1, tokens_out=1),
                stop_reason="stop",
                provider=req.provider,
                model=req.model,
            )

    monkeypatch.setattr(
        "oc8.modelrouter.get_model_router", lambda: _InadequateRouter()
    )

    async with app_session(tenant) as db:
        _auto, _fast, balanced, _strong = await _seed_tiers(db, tenant)
        long_ok = (
            "Here is a long draft answer that passes the length heuristic but "
            "should still be rejected by the self-check for missing steps."
        )
        verdict = await cascade_should_escalate(
            db,
            tenant_id=tenant,
            agent_id=None,
            answer=long_ok,
            messages=[NeutralMessage(role="user", content="Explain the migration plan")],
            verifier_config=balanced,
        )
        assert verdict.escalate is True
        assert verdict.via == "self_check"
        assert "missing" in verdict.reason.lower() or verdict.reason == "inadequate"


@pytest.mark.asyncio
async def test_cascade_self_check_keeps_adequate_answer(
    app_session: AppSessionFactory,
    platform_llm_keys: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))

    class _AdequateRouter:
        async def complete(self, req: Any) -> Any:
            from oc8.modelrouter import CompletionResult, Usage

            return CompletionResult(
                text='{"adequate": true, "reason": "covers the ask"}',
                tool_calls=[],
                usage=Usage(tokens_in=1, tokens_out=1),
                stop_reason="stop",
                provider=req.provider,
                model=req.model,
            )

    monkeypatch.setattr("oc8.modelrouter.get_model_router", lambda: _AdequateRouter())

    async with app_session(tenant) as db:
        _auto, _fast, balanced, _strong = await _seed_tiers(db, tenant)
        long_ok = (
            "Here is a long draft answer that passes the length heuristic and "
            "is judged adequate by the self-check, so no escalate."
        )
        verdict = await cascade_should_escalate(
            db,
            tenant_id=tenant,
            agent_id=None,
            answer=long_ok,
            messages=[NeutralMessage(role="user", content="Say hello politely")],
            verifier_config=balanced,
        )
        assert verdict.escalate is False
        assert verdict.via == "self_check"


def test_cascade_heuristic_still_short_circuits() -> None:
    # No async / no LLM: short answers escalate via heuristic alone.
    assert answer_looks_uncertain("short") is True


def test_record_preference_label_ring_buffer_and_merge() -> None:
    auto = m.ModelConfig(
        tenant_id=uuid.uuid4(),
        provider=AUTO_PROVIDER,
        model=AUTO_MODEL,
        locality="cloud",
        params={
            "preference_router": True,
            "preference_examples": [{"text": "seed example text here", "needs_strong": False}],
            "tiers": {"fast": str(uuid.uuid4())},
        },
    )
    msgs = [
        NeutralMessage(
            role="user",
            content="Please migrate the billing schema carefully and verify constraints",
        )
    ]
    assert record_preference_label(
        auto, messages=msgs, needs_strong=True, source="cascade:self_check"
    )
    assert record_preference_label(
        auto,
        messages=[
            NeutralMessage(role="user", content="Just say hello to the customer politely today")
        ],
        needs_strong=False,
        source="task_success_weak",
    )
    learned = auto.params["preference_learned"]
    assert len(learned) == 2
    assert learned[0]["needs_strong"] is True
    merged = preference_examples_for(auto)
    assert len(merged) == 3  # 1 seed + 2 learned


@pytest.mark.asyncio
async def test_learned_preference_raises_tier_on_similar_prompt(
    app_session: AppSessionFactory, platform_llm_keys: None
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, _fast, _balanced, strong = await _seed_tiers(db, tenant)
        auto.params = {
            **(auto.params or {}),
            "preference_router": True,
            "preference_learned": [
                {
                    "text": "migrate billing schema carefully verify constraints",
                    "needs_strong": True,
                    "source": "cascade:self_check",
                }
            ],
        }
        await db.flush()
        concrete, decision = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=None,
            run=None,
            messages=[
                NeutralMessage(
                    role="user",
                    content="Please migrate the billing schema carefully and verify constraints",
                )
            ],
        )
        assert concrete.id == strong.id
        assert decision.preference_score is not None
        assert decision.preference_score >= 0.55


@pytest.mark.asyncio
async def test_agent_affinity_skips_rediscovery_on_next_resolve(
    app_session: AppSessionFactory, platform_llm_keys: None
) -> None:
    """Once an agent settled on strong, the next run starts there — no cheap retry."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        auto, fast, _balanced, strong = await _seed_tiers(db, tenant)
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Sticky",
            definition={},
            presentation={},
        )
        db.add(agent)
        await db.flush()

        store_agent_affinity(agent, tier="strong", config_id=strong.id)
        assert agent_affinity(agent)["tier"] == "strong"

        concrete, decision = await resolve_auto_config(
            db,
            auto,
            tenant_id=tenant,
            agent_id=agent.id,
            agent=agent,
            run=None,
            messages=[NeutralMessage(role="user", content="hi")],
        )
        assert concrete.id == strong.id
        assert decision.reason == "agent_affinity"
        assert decision.signals.get("agent_affinity") == "strong"

        # Affinity never downgrades from a weaker success.
        remember_agent_route(agent, auto, run=None, concrete=fast)
        assert agent_affinity(agent)["tier"] == "strong"


