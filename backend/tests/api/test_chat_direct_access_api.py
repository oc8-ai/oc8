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
from oc8.authz.permissions import APPROVAL_DECIDE, COPILOT_USE, RUN_START
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


async def test_an_approved_write_the_department_still_holds_shows_as_waiting(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Person first, then the department: the person's approved card stays in
    their listing as waiting for the department, until it decides. They never
    see the department's own card."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna = _h(tenant, "anna")
            copilot_id = (await c.get("/api/v1/assistant", headers=anna)).json()["agentId"]
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": copilot_id}, headers=anna)
            ).json()["id"]
            with patch("oc8.runtime.intake.publish_run"):
                await c.post(
                    f"/api/v1/chat/sessions/{sid}/messages", json={"message": "hello"}, headers=anna
                )
            offer_id, run_id = await _seed(app_session, tenant, sid, copilot_id)
            async with app_session(tenant) as db:
                offer = await db.get(m.ApprovalRequest, offer_id)
                assert offer is not None
                dept_id = (
                    await db.execute(select(m.Department.id).where(m.Department.name == "Sales"))
                ).scalar_one()
                offer.status = "approved"
                held = m.ApprovalRequest(
                    tenant_id=tenant,
                    agent_id=offer.agent_id,
                    task_id=offer.task_id,
                    action_type="tool_send",
                    title="Copilot on behalf of Anna: change things (update_record)",
                    status="pending",
                    payload={
                        "run_id": str(run_id),
                        "tool": "update_record",
                        "arguments": {"id": 7},
                        "direct_access": {
                            **offer.payload["direct_access"],
                            "department_id": str(dept_id),
                        },
                    },
                )
                db.add(held)
                await db.flush()
                held_id = held.id
                await db.commit()

            url = f"/api/v1/chat/direct-access/approvals/{held_id}/decision"
            with patch("oc8.api.v1.chat_direct_access.publish_run") as published:
                ok = await c.post(url, json={"decision": "approve"}, headers=anna)
            assert ok.status_code == 200, ok.text
            body = ok.json()
            assert body["resumed"] is False and not published.called
            assert body["directAccess"]["awaitingDepartment"] is True

            mine = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            waiting = [a for a in mine if a["id"] == str(held_id)]
            assert len(waiting) == 1
            assert waiting[0]["status"] == "approved"
            assert waiting[0]["directAccess"]["awaitingDepartment"] is True
            # Only her own cards: the department's card is not in her listing.
            assert {a["id"] for a in mine} <= {str(offer_id), str(held_id)}


async def _with_agent(
    app_session: AppSessionFactory, tenant: uuid.UUID, offer_id: uuid.UUID
) -> str:
    """Anna may start runs where she sits; the offer names Sales' Quote Writer."""
    async with app_session(tenant) as db:
        anna = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.subject == "anna"))
        ).scalar_one()
        role = m.Role(tenant_id=tenant, name="runs", kind="human")
        db.add(role)
        await db.flush()
        for permission in (COPILOT_USE, RUN_START):
            db.add(m.RolePermission(tenant_id=tenant, role_id=role.id, permission=permission))
        anna.role_id = role.id
        dept_id = (
            await db.execute(select(m.Department.id).where(m.Department.name == "Sales"))
        ).scalar_one()
        writer = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="Quote Writer",
            role_title="",
            mission="Writes quotations",
            narrowing={},
            status="idle",
        )
        db.add(writer)
        await db.flush()
        offer = await db.get(m.ApprovalRequest, offer_id)
        assert offer is not None
        offer.payload = {
            **offer.payload,
            "direct_access": {
                **offer.payload["direct_access"],
                "login_name": "Sales login",
                "agents": [
                    {
                        "id": str(writer.id),
                        "name": "Quote Writer",
                        "department": "Sales",
                        "score": 0.8,
                    }
                ],
            },
        }
        await db.commit()
        return str(writer.id)


async def _anna_chat(c: AsyncClient, anna: dict[str, str]) -> tuple[str, str]:
    copilot_id = (await c.get("/api/v1/assistant", headers=anna)).json()["agentId"]
    sid = (
        await c.post("/api/v1/chat/sessions", json={"agentId": copilot_id}, headers=anna)
    ).json()["id"]
    with patch("oc8.runtime.intake.publish_run"):
        await c.post(
            f"/api/v1/chat/sessions/{sid}/messages", json={"message": "hello"}, headers=anna
        )
    return copilot_id, sid


async def test_decision_with_agent_id_starts_the_agent(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna = _h(tenant, "anna")
            copilot_id, sid = await _anna_chat(c, anna)
            offer_id, run_id = await _seed(app_session, tenant, sid, copilot_id)
            writer_id = await _with_agent(app_session, tenant, offer_id)

            url = f"/api/v1/chat/direct-access/approvals/{offer_id}/decision"
            with patch("oc8.api.v1.chat_direct_access.publish_run") as published:
                ok = await c.post(
                    url, json={"decision": "approve", "agentId": writer_id}, headers=anna
                )
            assert ok.status_code == 200, ok.text
            chosen = ok.json()["directAccess"]["chosenAgent"]
            assert chosen["id"] == writer_id and chosen["name"] == "Quote Writer"
            published_runs = [call.kwargs["run_id"] for call in published.call_args_list]
            assert published_runs == [uuid.UUID(chosen["runId"]), run_id]
            assert (
                await c.get(f"/api/v1/chat/sessions/{sid}/direct-access", headers=anna)
            ).json() == []

            again = await c.post(
                url, json={"decision": "approve", "agentId": writer_id}, headers=anna
            )
            assert again.status_code == 409
            async with app_session(tenant) as db:
                runs = (
                    await db.execute(select(m.AgentRun).where(m.AgentRun.source == "delegation"))
                ).scalars()
                assert [r.agent_id for r in runs] == [uuid.UUID(writer_id)]


async def test_an_agent_the_person_may_no_longer_run_is_refused_and_the_card_stays(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna = _h(tenant, "anna")
            copilot_id, sid = await _anna_chat(c, anna)
            offer_id, _run_id = await _seed(app_session, tenant, sid, copilot_id)
            writer_id = await _with_agent(app_session, tenant, offer_id)
            async with app_session(tenant) as db:
                writer = await db.get(m.Agent, uuid.UUID(writer_id))
                assert writer is not None
                writer.status = "paused"
                await db.commit()

            url = f"/api/v1/chat/direct-access/approvals/{offer_id}/decision"
            with patch("oc8.api.v1.chat_direct_access.publish_run") as published:
                refused = await c.post(
                    url, json={"decision": "approve", "agentId": writer_id}, headers=anna
                )
            assert refused.status_code == 403, refused.text
            assert refused.json()["detail"] == {"code": "agent_not_runnable"}
            assert not published.called
            mine = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            assert [(a["id"], a["status"]) for a in mine] == [(str(offer_id), "pending")]
            not_offered = await c.post(
                url, json={"decision": "approve", "agentId": str(uuid.uuid4())}, headers=anna
            )
            assert not_offered.status_code == 400
            assert not_offered.json()["detail"] == {"code": "agent_not_offered"}


async def test_the_card_dto_carries_agents_login_and_chosen_agent(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna = _h(tenant, "anna")
            copilot_id, sid = await _anna_chat(c, anna)
            offer_id, _run_id = await _seed(app_session, tenant, sid, copilot_id)
            writer_id = await _with_agent(app_session, tenant, offer_id)

            (card,) = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            info = card["directAccess"]
            assert info["agents"] == [
                {"id": writer_id, "name": "Quote Writer", "department": "Sales", "score": 0.8}
            ]
            assert info["loginName"] == "Sales login" and info["chosenAgent"] is None

            url = f"/api/v1/chat/direct-access/approvals/{offer_id}/decision"
            with patch("oc8.api.v1.chat_direct_access.publish_run"):
                await c.post(url, json={"decision": "approve", "agentId": writer_id}, headers=anna)
            (reloaded,) = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            assert reloaded["status"] == "approved"
            assert reloaded["directAccess"]["chosenAgent"]["id"] == writer_id
            assert reloaded["directAccess"]["chosenAgent"]["runId"]


async def test_choosing_an_agent_deleted_after_the_offer_is_refused(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    import datetime as dt

    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna = _h(tenant, "anna")
            copilot_id, sid = await _anna_chat(c, anna)
            offer_id, _run_id = await _seed(app_session, tenant, sid, copilot_id)
            writer_id = await _with_agent(app_session, tenant, offer_id)
            async with app_session(tenant) as db:
                writer = await db.get(m.Agent, uuid.UUID(writer_id))
                assert writer is not None
                writer.deleted_at = dt.datetime.now(dt.UTC)
                await db.commit()

            url = f"/api/v1/chat/direct-access/approvals/{offer_id}/decision"
            refused = await c.post(
                url, json={"decision": "approve", "agentId": writer_id}, headers=anna
            )
            assert refused.status_code == 403, refused.text
            assert refused.json()["detail"] == {"code": "agent_not_runnable"}
            mine = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            assert [(a["id"], a["status"]) for a in mine] == [(str(offer_id), "pending")]


async def test_the_listing_narrows_to_one_chat(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna = _h(tenant, "anna")
            copilot_id, sid = await _anna_chat(c, anna)
            offer_id, _run_id = await _seed(app_session, tenant, sid, copilot_id)
            other = (
                await c.post("/api/v1/chat/sessions", json={"agentId": copilot_id}, headers=anna)
            ).json()["id"]
            url = "/api/v1/chat/direct-access/approvals"
            own = (await c.get(url, params={"sessionId": sid}, headers=anna)).json()
            assert [a["id"] for a in own] == [str(offer_id)]
            assert (await c.get(url, params={"sessionId": other}, headers=anna)).json() == []


async def test_an_inbox_approve_grants_direct_access_only(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The person's own offer also sits in their approvals inbox, whose plain
    Approve sends no agent: it is the direct read grant, under the neutral
    title, and starts no agent."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna = _h(tenant, "anna")
            copilot_id, sid = await _anna_chat(c, anna)
            offer_id, run_id = await _seed(app_session, tenant, sid, copilot_id)
            await _with_agent(app_session, tenant, offer_id)
            async with app_session(tenant) as db:
                role = (await db.execute(select(m.Role).where(m.Role.name == "runs"))).scalar_one()
                db.add(
                    m.RolePermission(tenant_id=tenant, role_id=role.id, permission=APPROVAL_DECIDE)
                )
                await db.commit()

            inbox = (await c.get("/api/v1/approvals", headers=anna)).json()
            (listed,) = [a for a in inbox if a["id"] == str(offer_id)]
            assert "run it" not in listed["title"]
            assert listed["title"] == "Copilot on behalf of Anna: read things"

            with patch("oc8.runtime.intake.publish_run") as published:
                ok = await c.post(
                    f"/api/v1/approvals/{offer_id}/decision",
                    json={"decision": "approve"},
                    headers=anna,
                )
            assert ok.status_code == 200, ok.text
            assert ok.json()["directAccess"]["chosenAgent"] is None
            assert [call.kwargs["run_id"] for call in published.call_args_list] == [run_id]
            grants = (
                await c.get(f"/api/v1/chat/sessions/{sid}/direct-access", headers=anna)
            ).json()
            assert [g["toolKey"] for g in grants] == ["things"]
            async with app_session(tenant) as db:
                delegations = (
                    await db.execute(select(m.AgentRun).where(m.AgentRun.source == "delegation"))
                ).scalars()
                assert list(delegations) == []
                run = await db.get(m.AgentRun, run_id)
                assert run is not None
                answer = run.context["resolved_direct_access"]
                assert answer["decision"] == "approve" and "chosen_agent" not in answer


async def test_an_agent_on_a_held_write_card_is_a_bad_request(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Only an offer starts a chosen agent: an agentId on a held write is the
    client's mistake (400), not a server error, and decides nothing."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            anna = _h(tenant, "anna")
            copilot_id, sid = await _anna_chat(c, anna)
            offer_id, run_id = await _seed(app_session, tenant, sid, copilot_id)
            async with app_session(tenant) as db:
                offer = await db.get(m.ApprovalRequest, offer_id)
                assert offer is not None
                held = m.ApprovalRequest(
                    tenant_id=tenant,
                    agent_id=offer.agent_id,
                    task_id=offer.task_id,
                    action_type="tool_send",
                    title="Copilot on behalf of Anna: change things (update_record)",
                    status="pending",
                    payload={
                        "run_id": str(run_id),
                        "tool": "update_record",
                        "arguments": {"id": 7},
                        "direct_access": dict(offer.payload["direct_access"]),
                    },
                )
                db.add(held)
                await db.commit()
                held_id = held.id

            url = f"/api/v1/chat/direct-access/approvals/{held_id}/decision"
            refused = await c.post(
                url, json={"decision": "approve", "agentId": str(uuid.uuid4())}, headers=anna
            )
            assert refused.status_code == 400, refused.text
            mine = (await c.get("/api/v1/chat/direct-access/approvals", headers=anna)).json()
            assert [a["status"] for a in mine if a["id"] == str(held_id)] == ["pending"]
