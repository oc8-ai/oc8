"""GET /agents/{id}/live (spec §3.1): who may read it, who may switch on
"All runs", and that the wire never carries a colleague's chat text."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _agent(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        return agent.id


async def _bobs_chat_and_a_schedule(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Bob's chat run (+ a sub-run it delegated, + an attachment on his
    message), and one scheduled run."""
    async with app_session(tenant) as db:
        bob = m.OrgMember(
            tenant_id=tenant, subject="bob", subject_uuid=uuid.uuid4(), display_name="Bob"
        )
        db.add(bob)
        await db.flush()
        session = m.ChatSession(tenant_id=tenant, agent_id=agent_id, member_id=bob.id)
        db.add(session)
        await db.flush()
        msg = m.ChatMessage(
            tenant_id=tenant, session_id=session.id, role="user", content="BOB SECRET"
        )
        db.add(msg)
        await db.flush()
        db.add(
            m.FileAttachment(
                tenant_id=tenant,
                owner_type="chat_message",
                owner_id=msg.id,
                filename="bob-payslip.pdf",
                content_type="application/pdf",
                size_bytes=3,
                bucket_key=f"t/{uuid.uuid4()}",
            )
        )
        chat_ctx = {
            "chat_session_id": str(session.id),
            "originating_operator": "bob",
            "task": "User: BOB SECRET",
        }
        bob_run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent_id,
            source="chat",
            state="done",
            context={**chat_ctx, "output": "the answer", "steps": 2},
        )
        sub_run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent_id,
            source="delegation",
            state="done",
            context={**chat_ctx, "task": "Lead says: BOB SECRET"},
        )
        cron = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent_id,
            source="cron",
            state="done",
            context={"task": "check inbox", "output": "2 new"},
        )
        db.add_all([bob_run, sub_run, cron])
        await db.flush()
        return bob_run.id, sub_run.id, cron.id


def _run_ids(body: dict[str, Any]) -> set[str]:
    return {item["run"]["id"] for item in body["items"] if item["run"] is not None}


async def test_all_runs_needs_manage_rights(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        admin = await c.get(
            f"/api/v1/agents/{agent_id}/live?allRuns=true", headers=_h(tenant, "admin", "org_admin")
        )
        assert admin.status_code == 200, admin.text
        assert admin.json()["mayViewAllRuns"] is True
        viewer = await c.get(
            f"/api/v1/agents/{agent_id}/live?allRuns=true", headers=_h(tenant, "aud", "auditor")
        )
        assert viewer.status_code == 403
        assert "All runs needs the right to manage this agent" in viewer.text
        plain = await c.get(f"/api/v1/agents/{agent_id}/live", headers=_h(tenant, "aud", "auditor"))
        assert plain.status_code == 200, plain.text
        assert plain.json()["mayViewAllRuns"] is False


async def test_the_caller_must_reach_the_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.get(f"/api/v1/agents/{agent_id}/live", headers=_h(tenant, "emp", "member"))
        assert r.status_code == 404
        missing = await c.get(
            f"/api/v1/agents/{uuid.uuid4()}/live", headers=_h(tenant, "admin", "org_admin")
        )
        assert missing.status_code == 404


async def test_a_colleagues_chat_is_absent_and_all_runs_shows_no_words(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    bob_run, sub_run, cron = await _bobs_chat_and_a_schedule(app_session, tenant, agent_id)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        url = f"/api/v1/agents/{agent_id}/live"
        plain = await c.get(url, headers=_h(tenant, "admin", "org_admin"))
        assert plain.status_code == 200, plain.text
        assert _run_ids(plain.json()) == {str(cron)}
        everything = await c.get(f"{url}?allRuns=true", headers=_h(tenant, "admin", "org_admin"))
        assert everything.status_code == 200, everything.text
    body = everything.json()
    assert _run_ids(body) == {str(cron), str(bob_run), str(sub_run)}
    by_id = {i["run"]["id"]: i["run"] for i in body["items"] if i["run"]}
    # Steps and results, yes; who started it, yes.
    assert by_id[str(bob_run)]["output"] == "the answer"
    assert by_id[str(bob_run)]["steps"] == 2
    assert by_id[str(bob_run)]["startedBy"] == "Bob"
    # His words, his task text, his attachment: nowhere on the wire.
    for forbidden in ("BOB SECRET", "bob-payslip", "task", "context"):
        assert forbidden not in everything.text
        assert forbidden not in plain.text


async def test_the_members_turns_page_back_with_a_cursor(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        url = f"/api/v1/agents/{agent_id}/live"
        headers = _h(tenant, "admin", "org_admin")
        first = (await c.get(url, headers=headers)).json()
        session_id = uuid.UUID(first["sessionId"])
        assert first["items"] == [] and first["nextBefore"] is None
        async with app_session(tenant) as db:
            base = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
            for i in range(3):
                db.add(
                    m.ChatMessage(
                        tenant_id=tenant,
                        session_id=session_id,
                        role="user",
                        content=f"m{i}",
                        created_at=base + dt.timedelta(minutes=i),
                    )
                )
        page = (await c.get(f"{url}?limit=2", headers=headers)).json()
        assert page["sessionId"] == str(session_id)
        assert [i["message"]["content"] for i in page["items"]] == ["m1", "m2"]
        assert page["nextBefore"]
        older = await c.get(url, params={"limit": 2, "before": page["nextBefore"]}, headers=headers)
        assert older.status_code == 200, older.text
        assert [i["message"]["content"] for i in older.json()["items"]] == ["m0"]
        assert older.json()["nextBefore"] is None
