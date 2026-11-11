"""A chat's reasoning level rides on the run and is folded into the pinned
definition by resolve_version, at the model override's seam (spec §7)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agents.versioning import resolve_version
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _agent_and_run(
    app_session: AppSessionFactory, tenant: uuid.UUID, context: dict[str, Any]
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="A",
            definition={"model_params": {"temperature": 0.9}},
        )
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="chat", context=context)
        db.add(run)
        await db.flush()
        return agent.id, run.id


async def _payload(
    app_session: AppSessionFactory, tenant: uuid.UUID, context: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    agent_id, run_id = await _agent_and_run(app_session, tenant, context)
    async with app_session(tenant) as db:
        agent, run = await db.get(m.Agent, agent_id), await db.get(m.AgentRun, run_id)
        assert agent is not None and run is not None
        payload = await resolve_version(db, run, agent)
        return payload, dict(agent.definition)


async def test_the_level_lands_in_the_pinned_definition(app_session: AppSessionFactory) -> None:
    payload, live = await _payload(app_session, uuid.uuid4(), {"effort_override": "thorough"})
    assert payload["definition"]["model_params"] == {"temperature": 0.9, "reasoning": "thorough"}
    assert live == {"model_params": {"temperature": 0.9}}, "the agent row is never touched"


async def test_an_unknown_value_is_ignored(app_session: AppSessionFactory) -> None:
    payload, _ = await _payload(app_session, uuid.uuid4(), {"effort_override": "max"})
    assert "reasoning" not in payload["definition"]["model_params"]


async def test_a_run_without_a_level_is_untouched(app_session: AppSessionFactory) -> None:
    payload, _ = await _payload(app_session, uuid.uuid4(), {})
    assert payload["definition"]["model_params"] == {"temperature": 0.9}


async def test_a_turn_carries_the_chats_level(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        agent_id = agent.id
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = (
                await c.post(
                    "/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=headers
                )
            ).json()["id"]
            await c.put(
                f"/api/v1/chat/sessions/{sid}/effort", json={"effort": "fast"}, headers=headers
            )
            with patch("oc8.runtime.intake.publish_run"):
                sent = await c.post(
                    f"/api/v1/chat/sessions/{sid}/messages",
                    json={"message": "hi"},
                    headers=headers,
                )
            assert sent.status_code == 201, sent.text
    async with app_session(tenant) as db:
        run = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id))
        ).scalar_one()
        assert run.context["effort_override"] == "fast"
