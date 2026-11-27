"""Always allow (for me) over HTTP: made with the person's own approval,
refused on every other card, listed and revoked on the profile -- the
caller's own rules only."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER
from oc8.authz.scope import subject_uuid_for
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

NOT_ELIGIBLE = "always allow covers only your own write-tier approval"


def _h(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


async def _start(c: AsyncClient, tenant: uuid.UUID) -> tuple[str, str]:
    """Anna's Copilot conversation with one turn. Returns (copilot id, session id)."""
    anna = _h(tenant, "anna")
    copilot_id = (await c.get("/api/v1/assistant", headers=anna)).json()["agentId"]
    sid = (
        await c.post("/api/v1/chat/sessions", json={"agentId": copilot_id}, headers=anna)
    ).json()["id"]
    with patch("oc8.runtime.intake.publish_run"):
        await c.post(f"/api/v1/chat/sessions/{sid}/messages", json={"message": "hi"}, headers=anna)
    return copilot_id, sid


async def _held_write(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    session_id: str,
    copilot_id: str,
    *,
    tier: str = "write",
    decider: str = "member",
) -> uuid.UUID:
    """A Sales seat, the connected `things`, a parked run and the person's
    held write on it (or, `decider="department"`, the department's stage)."""
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
                tenant_id=tenant, member_id=anna.id, department_id=dept.id, seat_role="dept_viewer"
            )
        )
        conn = m.McpConnection(
            tenant_id=tenant,
            department_id=dept.id,
            name="things",
            transport="stdio",
            server_url="stdio://things",
            connected=True,
            config={},
            scopes={},
        )
        db.add(conn)
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
        info: dict[str, Any] = {
            "member_id": str(anna.id),
            "member_name": "Anna",
            "session_id": session_id,
            "tool_key": "things",
            "label": "things",
            "account": "organisation",
            "decider": decider,
            "department_id": str(dept.id),
        }
        if decider == "department":
            info["member_approval_id"] = str(uuid.uuid4())
        card = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.UUID(copilot_id),
            task_id=session.task_id,
            department_id=dept.id if decider == "department" else None,
            action_type="tool_send",
            title="Copilot on behalf of Anna: change things (update_record)",
            status="pending",
            payload={
                "run_id": str(run.id),
                "tool": "update_record",
                "arguments": {"id": 7},
                "risk_tier": tier,
                "connection_id": str(conn.id),
                "direct_access": info
                if decider == "department"
                else {**info, "department_id": None},
            },
        )
        db.add(card)
        await db.flush()
        card_id = card.id
        await db.commit()
    return card_id


async def _rules(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.ToolAllowRule]:
    async with app_session(tenant) as db:
        return list((await db.execute(select(m.ToolAllowRule))).scalars())


async def _status(app_session: AppSessionFactory, tenant: uuid.UUID, card_id: uuid.UUID) -> str:
    async with app_session(tenant) as db:
        card = await db.get(m.ApprovalRequest, card_id)
        assert card is not None
        return card.status


async def test_approve_with_always_allow_creates_a_rule(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            copilot_id, sid = await _start(c, tenant)
            card_id = await _held_write(app_session, tenant, sid, copilot_id)
            anna = _h(tenant, "anna")
            listed = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            assert [a["alwaysAllowOffered"] for a in listed if a["id"] == str(card_id)] == [True]

            url = f"/api/v1/chat/direct-access/approvals/{card_id}/decision"
            with patch("oc8.api.v1.chat_direct_access.publish_run"):
                ok = await c.post(
                    url, json={"decision": "approve", "alwaysAllow": True}, headers=anna
                )
            assert ok.status_code == 200, ok.text

            rules = (await c.get("/api/v1/tool-allow-rules", headers=anna)).json()
            assert len(rules) == 1
            (rule,) = rules
            assert rule["agentId"] == copilot_id
            assert rule["agentName"]
            assert rule["connectionName"] == "things"
            assert rule["toolKey"] == "update_record"
            (row,) = await _rules(app_session, tenant)
            assert row.created_from_approval_id == card_id
    assert await _status(app_session, tenant, card_id) == "approved"


@pytest.mark.parametrize("tier", ["destructive", "irreversible", "outward"])
async def test_always_allow_is_refused_for_destructive_irreversible_outward(
    app_session: AppSessionFactory, redis_url: str, tier: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            copilot_id, sid = await _start(c, tenant)
            card_id = await _held_write(app_session, tenant, sid, copilot_id, tier=tier)
            anna = _h(tenant, "anna")
            listed = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            assert [a["alwaysAllowOffered"] for a in listed if a["id"] == str(card_id)] == [False]
            url = f"/api/v1/chat/direct-access/approvals/{card_id}/decision"
            with patch("oc8.api.v1.chat_direct_access.publish_run") as published:
                refused = await c.post(
                    url, json={"decision": "approve", "alwaysAllow": True}, headers=anna
                )
            assert refused.status_code == 400, refused.text
            assert refused.json()["detail"] == NOT_ELIGIBLE
            assert not published.called
    assert await _rules(app_session, tenant) == []
    # Nothing was decided either: the card still asks.
    assert await _status(app_session, tenant, card_id) == "pending"


async def test_always_allow_is_refused_on_a_department_card(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            copilot_id, sid = await _start(c, tenant)
            card_id = await _held_write(app_session, tenant, sid, copilot_id, decider="department")
            hos = _h(tenant, "hos")
            # The department's approver: a Sales approver seat.
            async with app_session(tenant) as db:
                member = m.OrgMember(
                    tenant_id=tenant, subject="hos", subject_uuid=subject_uuid_for("hos")
                )
                db.add(member)
                await db.flush()
                dept_id = (
                    await db.execute(select(m.Department.id).where(m.Department.name == "Sales"))
                ).scalar_one()
                db.add(
                    m.OrgMemberDepartment(
                        tenant_id=tenant,
                        member_id=member.id,
                        department_id=dept_id,
                        seat_role=SEAT_APPROVER,
                    )
                )
                await db.commit()
            refused = await c.post(
                f"/api/v1/approvals/{card_id}/decision",
                json={"decision": "approve", "alwaysAllow": True},
                headers=hos,
            )
            assert refused.status_code == 400, refused.text
            assert refused.json()["detail"] == NOT_ELIGIBLE
    assert await _rules(app_session, tenant) == []
    assert await _status(app_session, tenant, card_id) == "pending"


async def test_revoke_of_another_members_rule_is_404(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            copilot_id, sid = await _start(c, tenant)
            card_id = await _held_write(app_session, tenant, sid, copilot_id)
            anna, eve = _h(tenant, "anna"), _h(tenant, "eve", role="org_admin")
            with patch("oc8.api.v1.chat_direct_access.publish_run"):
                ok = await c.post(
                    f"/api/v1/chat/direct-access/approvals/{card_id}/decision",
                    json={"decision": "approve", "alwaysAllow": True},
                    headers=anna,
                )
            assert ok.status_code == 200, ok.text
            (rule,) = (await c.get("/api/v1/tool-allow-rules", headers=anna)).json()
            # Not even an administrator sees or revokes somebody else's rule.
            assert (await c.get("/api/v1/tool-allow-rules", headers=eve)).json() == []
            url = f"/api/v1/tool-allow-rules/{rule['id']}"
            assert (await c.delete(url, headers=eve)).status_code == 404
            assert len((await c.get("/api/v1/tool-allow-rules", headers=anna)).json()) == 1
            assert (
                await c.delete(f"/api/v1/tool-allow-rules/{uuid.uuid4()}", headers=anna)
            ).status_code == 404

            assert (await c.delete(url, headers=anna)).status_code == 204
            assert (await c.get("/api/v1/tool-allow-rules", headers=anna)).json() == []
            assert (await c.delete(url, headers=anna)).status_code == 404
    (row,) = await _rules(app_session, tenant)
    assert row.revoked_at is not None
