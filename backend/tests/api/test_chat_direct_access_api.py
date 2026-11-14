"""The chat's own door to direct access: only the conversation's member sees,
decides and revokes -- no seat needed."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    app_session: AppSessionFactory, tenant: uuid.UUID, session_id: str, copilot_id: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Sales seat + connected `things`, a parked run and its pending offer.
    Returns (offer id, run id)."""
    async with app_session(tenant) as db:
        anna = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.subject == "anna"))
        ).scalar_one()
        dept = m.Department(
            tenant_id=tenant,
            name="Sales",
            frame={"tools": {"things": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(dept)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=anna.id,
                department_id=dept.id,
                seat_role="dept_viewer",
            )
        )
        db.add(
            m.McpConnection(
                tenant_id=tenant,
                department_id=dept.id,
                name="things",
                transport="stdio",
                server_url="stdio://things",
                connected=True,
                config={},
                scopes={},
            )
        )
        session = await db.get(m.ChatSession, uuid.UUID(session_id))
        assert session is not None
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=uuid.UUID(copilot_id),
            task_id=session.task_id,
            source="chat",
            state="waiting_for_approval",
            context={"chat_session_id": session_id, "door": "web", "on_behalf_of": str(anna.id)},
        )
        db.add(run)
        await db.flush()
        offer = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.UUID(copilot_id),
            task_id=session.task_id,
            action_type="direct_access",
            title="Copilot on behalf of Anna: read things",
            status="pending",
            payload={
                "run_id": str(run.id),
                "tool": "offer_direct_access",
                "arguments": {"connection": "things", "purpose": "p"},
                "direct_access": {
                    "member_id": str(anna.id),
                    "member_name": "Anna",
                    "session_id": session_id,
                    "tool_key": "things",
                    "label": "things",
                    "account": "organisation",
                    "decider": "member",
                },
            },
        )
        db.add(offer)
        await db.flush()
        return offer.id, run.id


async def test_only_the_member_sees_decides_and_revokes(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna, eve = _h(tenant, "anna"), _h(tenant, "eve", role="org_admin")
            copilot_id = (await c.get("/api/v1/assistant", headers=anna)).json()["agentId"]
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": copilot_id}, headers=anna)
            ).json()["id"]
            with patch("oc8.runtime.intake.publish_run"):
                await c.post(
                    f"/api/v1/chat/sessions/{sid}/messages", json={"message": "hello"}, headers=anna
                )
            offer_id, _run_id = await _seed(app_session, tenant, sid, copilot_id)

            mine = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            assert [a["id"] for a in mine] == [str(offer_id)]
            assert mine[0]["directAccess"]["memberName"] == "Anna"
            assert (await c.get("/api/v1/chat/direct-access/approvals", headers=eve)).json() == []

            url = f"/api/v1/chat/direct-access/approvals/{offer_id}/decision"
            assert (await c.post(url, json={"decision": "approve"}, headers=eve)).status_code == 404
            with patch("oc8.api.v1.chat_direct_access.publish_run") as published:
                ok = await c.post(url, json={"decision": "approve"}, headers=anna)
            assert ok.status_code == 200, ok.text
            assert ok.json()["resumed"] is True and published.called

            grants_url = f"/api/v1/chat/sessions/{sid}/direct-access"
            grants = (await c.get(grants_url, headers=anna)).json()
            assert [g["toolKey"] for g in grants] == ["things"]
            assert (await c.get(grants_url, headers=eve)).status_code == 404
            again = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            assert again[0]["directAccess"]["grantActive"] is True

            revoke = f"{grants_url}/{grants[0]['id']}/revoke"
            assert (await c.post(revoke, headers=eve)).status_code == 404
            assert (await c.post(revoke, headers=anna)).status_code == 204
            assert (await c.get(grants_url, headers=anna)).json() == []
            assert (await c.post(revoke, headers=anna)).status_code == 404
