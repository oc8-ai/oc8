"""A chat's reasoning level: owner-only writes, strict values, and a model
picker that says what applies without a pick (spec 2026-11-11 §3, §8)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.edition.runtime import RuntimeComposition
from oc8.main import create_app
from oc8.runtime.supervision_hook import NOOP_SUPERVISION_RUN_HOOK
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID, subject: str = "op") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, agent_effort: str | None = None
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        thinker = m.ModelConfig(
            tenant_id=tenant,
            provider="anthropic",
            model="claude-sonnet-5",
            locality="cloud",
            supports_reasoning=True,
        )
        custom = m.ModelConfig(
            tenant_id=tenant,
            provider="openai",
            model="o3",
            locality="cloud",
            supports_reasoning=True,
            params={"effort": "max"},
        )
        plain = m.ModelConfig(
            tenant_id=tenant,
            provider="ollama",
            model="llama3.1:8b",
            locality="local",
        )
        db.add_all([thinker, custom, plain])
        await db.flush()
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        definition: dict[str, Any] = (
            {"model_params": {"effort": agent_effort}} if agent_effort else {}
        )
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="A",
            model_config_id=thinker.id,
            definition=definition,
        )
        db.add(agent)
        await db.flush()
        return agent.id, thinker.id, custom.id, plain.id


async def _session(c: AsyncClient, tenant: uuid.UUID, agent_id: uuid.UUID) -> str:
    r = await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=_h(tenant))
    sid: str = r.json()["id"]
    return sid


async def test_the_owner_sets_and_clears_a_level(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, *_ = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = await _session(c, tenant, agent_id)
            assert (await c.get(f"/api/v1/chat/sessions/{sid}", headers=_h(tenant))).json()[
                "effort"
            ] is None
            ok = await c.put(
                f"/api/v1/chat/sessions/{sid}/effort",
                json={"effort": "thorough"},
                headers=_h(tenant),
            )
            assert ok.status_code == 200 and ok.json()["effort"] == "thorough"
            detail = await c.get(f"/api/v1/chat/sessions/{sid}", headers=_h(tenant))
            assert detail.json()["effort"] == "thorough"
            cleared = await c.put(
                f"/api/v1/chat/sessions/{sid}/effort", json={"effort": None}, headers=_h(tenant)
            )
            assert cleared.json()["effort"] is None


@pytest.mark.parametrize("value", ["max", "high", "Fast", 3])
async def test_anything_but_a_level_is_a_422(app_session: AppSessionFactory, value: Any) -> None:
    tenant = uuid.uuid4()
    agent_id, *_ = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = await _session(c, tenant, agent_id)
            r = await c.put(
                f"/api/v1/chat/sessions/{sid}/effort", json={"effort": value}, headers=_h(tenant)
            )
    assert r.status_code == 422, r.text


async def test_only_the_owner_may_set_it(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, *_ = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = await _session(c, tenant, agent_id)
            r = await c.put(
                f"/api/v1/chat/sessions/{sid}/effort",
                json={"effort": "fast"},
                headers=_h(tenant, subject="someone-else"),
            )
    assert r.status_code == 404, r.text


async def test_the_pick_survives_a_model_switch(app_session: AppSessionFactory) -> None:
    """Switching to a model that cannot think hides the control; the pick is
    kept for when the chat is back on one that can (spec §3)."""
    tenant = uuid.uuid4()
    agent_id, thinker, _custom, plain = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = await _session(c, tenant, agent_id)
            await c.put(
                f"/api/v1/chat/sessions/{sid}/effort", json={"effort": "fast"}, headers=_h(tenant)
            )
            to_plain = await c.put(
                f"/api/v1/chat/sessions/{sid}/model",
                json={"modelConfigId": str(plain)},
                headers=_h(tenant),
            )
            assert to_plain.json()["effort"] == "fast"
            back = await c.put(
                f"/api/v1/chat/sessions/{sid}/model",
                json={"modelConfigId": str(thinker)},
                headers=_h(tenant),
            )
            assert back.json()["effort"] == "fast"


async def test_the_options_say_whether_each_model_thinks_and_what_applies(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, thinker, custom, plain = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = await _session(c, tenant, agent_id)
            by_session = (
                await c.get(f"/api/v1/chat/sessions/{sid}/models", headers=_h(tenant))
            ).json()
            by_agent = (
                await c.get(f"/api/v1/chat/agents/{agent_id}/models", headers=_h(tenant))
            ).json()
    assert by_session == by_agent
    by_id = {o["id"]: o for o in by_session}
    # Auto runs on the agent's default model (the thinker).
    assert (by_id[None]["supportsReasoning"], by_id[None]["defaultEffort"]) == (True, "balanced")
    assert (by_id[str(thinker)]["supportsReasoning"], by_id[str(thinker)]["defaultEffort"]) == (
        True,
        "balanced",
    )
    assert by_id[str(custom)]["defaultEffort"] == "custom"
    assert by_id[str(plain)]["supportsReasoning"] is False


async def test_the_agents_configured_effort_is_the_default(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, thinker, custom, _plain = await _seed(app_session, tenant, agent_effort="low")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            options = (
                await c.get(f"/api/v1/chat/agents/{agent_id}/models", headers=_h(tenant))
            ).json()
    by_id = {o["id"]: o for o in options}
    assert by_id[None]["defaultEffort"] == "fast"
    # The agent's value is narrower than the model config's custom one.
    assert by_id[str(custom)]["defaultEffort"] == "fast"
    assert by_id[str(thinker)]["defaultEffort"] == "fast"


async def test_a_legacy_extra_reasoning_effort_is_the_default(
    app_session: AppSessionFactory,
) -> None:
    """An operator's raw `extra.reasoning_effort` from before the levels names
    what applies, so "Default · ..." matches the run."""
    tenant = uuid.uuid4()
    agent_id, *_ = await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        legacy = m.ModelConfig(
            tenant_id=tenant,
            provider="openai",
            model="o3-mini",
            locality="cloud",
            supports_reasoning=True,
            params={"extra": {"reasoning_effort": "high"}},
        )
        db.add(legacy)
        await db.flush()
        legacy_id = legacy.id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            options = (
                await c.get(f"/api/v1/chat/agents/{agent_id}/models", headers=_h(tenant))
            ).json()
    by_id = {o["id"]: o for o in options}
    assert by_id[str(legacy_id)]["defaultEffort"] == "thorough"


async def test_auto_on_an_auto_router_default_offers_no_level(
    app_session: AppSessionFactory,
) -> None:
    """An auto-router row only routes: whether a level applies is decided per
    routed model, so "Auto" on such an agent shows no control."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        router = m.ModelConfig(
            tenant_id=tenant,
            provider="auto",
            model="router",
            locality="cloud",
            supports_reasoning=True,
            params={"tiers": {}},
        )
        thinker = m.ModelConfig(
            tenant_id=tenant,
            provider="anthropic",
            model="claude-sonnet-5",
            locality="cloud",
            supports_reasoning=True,
        )
        db.add_all([router, thinker])
        await db.flush()
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="A", model_config_id=router.id
        )
        db.add(agent)
        await db.flush()
        agent_id, thinker_id = agent.id, thinker.id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            options = (
                await c.get(f"/api/v1/chat/agents/{agent_id}/models", headers=_h(tenant))
            ).json()
    by_id = {o["id"]: o for o in options}
    assert by_id[None]["supportsReasoning"] is False
    assert by_id[str(thinker_id)]["supportsReasoning"] is True


async def test_a_reader_sees_the_level_and_cannot_change_it(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        bob = await copilot_seat(db, tenant, "bob")
        chat = await db.get(m.ChatSession, alice.session_id)
        assert chat is not None
        chat.effort = "thorough"

    async def readers(_db: AsyncSession, session: m.ChatSession) -> list[uuid.UUID]:
        return [bob.member_id] if session.id == alice.session_id else []

    app = create_app(
        runtime_composition=RuntimeComposition(NOOP_SUPERVISION_RUN_HOOK, extra_readers=readers)
    )
    sid = alice.session_id
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            detail = await c.get(f"/api/v1/chat/sessions/{sid}", headers=_h(tenant, "bob"))
            write = await c.put(
                f"/api/v1/chat/sessions/{sid}/effort",
                json={"effort": "fast"},
                headers=_h(tenant, "bob"),
            )
    assert detail.status_code == 200, detail.text
    assert detail.json()["readOnly"] is True
    assert detail.json()["effort"] == "thorough"
    assert write.status_code == 404, write.text
