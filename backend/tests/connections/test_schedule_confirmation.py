"""A schedule acts through its creator's own account only while the agent is
what the creator last saw: someone else rewriting the agent must not get to
use that account unattended (connections design, schedule confirmation)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.connections.access import ConnectionRequired, resolve_tool_access
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, enable_plugin_row, headers, member, oauth_row
from tests.connections.test_access import MS_CFG
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio


async def _world(app_session: AppSessionFactory) -> tuple[m.Agent, uuid.UUID, uuid.UUID]:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    await member(app_session, tenant, "ben@acme.test", name="Ben")
    await enable_plugin_row(app_session, tenant, "microsoft365")
    await oauth_row(app_session, tenant, owner=anna)
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant, name="microsoft365", server_url="", transport="stdio", config=MS_CFG
        )
        db.add(conn)
        await db.flush()
        return agent, anna, conn.id


async def _resolve(
    app_session: AppSessionFactory, agent: m.Agent, conn_id: uuid.UUID, context: dict[str, Any]
) -> Any:
    async with app_session(agent.tenant_id) as db:
        run = m.AgentRun(
            tenant_id=agent.tenant_id, agent_id=agent.id, source="cron", context=context
        )
        db.add(run)
        await db.flush()
        conn = await db.get(m.McpConnection, conn_id)
        live = await db.get(m.Agent, agent.id)
        assert conn is not None and live is not None
        return await resolve_tool_access(db, run=run, agent=live, conn=conn)


async def _publish_new_version(app_session: AppSessionFactory, agent: m.Agent) -> None:
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        live.current_version_id = uuid.uuid4()


def _as(tenant: uuid.UUID, who: str) -> dict[str, str]:
    return headers(tenant, f"{who}@acme.test", "org_admin")


async def test_a_schedule_stops_acting_for_its_creator_after_someone_changes_the_agent(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn = await _world(app_session)
    tenant = agent.tenant_id
    async with api() as c:
        created = await c.post(
            f"/api/v1/agents/{agent.id}/triggers",
            json={"kind": "cron", "taskText": "Inbox sortieren", "cronExpression": "0 8 * * *"},
            headers=_as(tenant, "anna"),
        )
        assert created.status_code == 201, created.text
        trigger_id = created.json()["id"]
        assert created.json()["needsConfirmation"] is False
        ctx = {"trigger_id": trigger_id}
        assert (await _resolve(app_session, agent, conn, ctx)).on_behalf_of == anna

        await _publish_new_version(app_session, agent)  # Ben rewrote the agent
        with pytest.raises(ConnectionRequired) as exc:
            await _resolve(app_session, agent, conn, ctx)
        assert exc.value.reason == "schedule_needs_confirmation"
        # Delegated sub-runs and Copilot follow-ups carry the trigger: same rule.
        with pytest.raises(ConnectionRequired):
            await _resolve(app_session, agent, conn, {**ctx, "on_behalf_of": str(anna)})
        with pytest.raises(ConnectionRequired):
            await _resolve(
                app_session,
                agent,
                conn,
                {"originating_operator": "anna@acme.test", "followup": {"trigger_id": trigger_id}},
            )

        url = f"/api/v1/agents/{agent.id}/triggers"
        for_anna = (await c.get(url, headers=_as(tenant, "anna"))).json()
        for_ben = (await c.get(url, headers=_as(tenant, "ben"))).json()
        assert [t["needsConfirmation"] for t in for_anna] == [True]
        assert [t["needsConfirmation"] for t in for_ben] == [False]  # only its creator can

        # Saving it again as somebody else re-owns it to them -- never Anna's.
        saved = await c.patch(
            f"/api/v1/triggers/{trigger_id}",
            json={"taskText": "Inbox sortieren"},
            headers=_as(tenant, "anna"),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["needsConfirmation"] is False
    assert (await _resolve(app_session, agent, conn, ctx)).on_behalf_of == anna


async def test_someone_elses_save_does_not_confirm_for_the_creator(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn = await _world(app_session)
    tenant = agent.tenant_id
    async with api() as c:
        created = await c.post(
            f"/api/v1/agents/{agent.id}/triggers",
            json={"kind": "cron", "taskText": "t", "cronExpression": "0 8 * * *"},
            headers=_as(tenant, "anna"),
        )
        trigger_id = created.json()["id"]
        await _publish_new_version(app_session, agent)
        # Ben only switches it off: it stays Anna's, and stays unconfirmed.
        off = await c.patch(
            f"/api/v1/triggers/{trigger_id}", json={"enabled": False}, headers=_as(tenant, "ben")
        )
        assert off.status_code == 200, off.text
    with pytest.raises(ConnectionRequired) as exc:
        await _resolve(app_session, agent, conn, {"trigger_id": trigger_id})
    assert exc.value.reason == "schedule_needs_confirmation"


async def test_a_schedule_saved_before_confirmation_existed_needs_saving_again(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn = await _world(app_session)
    async with app_session(agent.tenant_id) as db:
        trigger = m.Trigger(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            kind="cron",
            task_text="t",
            cron_expression="0 8 * * *",
            created_by_member_id=anna,
        )
        db.add(trigger)
        await db.flush()
        trigger_id = str(trigger.id)
    with pytest.raises(ConnectionRequired) as exc:
        await _resolve(app_session, agent, conn, {"trigger_id": trigger_id})
    assert exc.value.reason == "schedule_needs_confirmation"
    async with app_session(agent.tenant_id) as db:
        assert (
            await db.scalar(
                select(m.Trigger.access_fingerprint).where(m.Trigger.agent_id == agent.id)
            )
        ) is None


async def test_a_chat_run_is_not_affected(app_session: AppSessionFactory) -> None:
    agent, anna, conn = await _world(app_session)
    await _publish_new_version(app_session, agent)
    access = await _resolve(app_session, agent, conn, {"originating_operator": "anna@acme.test"})
    assert access.on_behalf_of == anna
