"""A reader an edition admits can open a chat and nothing else (Ruling 45)."""

from __future__ import annotations

import uuid

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


def _headers(tenant: uuid.UUID, subject: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_reader_reads_and_cannot_write(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        bob = await copilot_seat(db, tenant, "bob")
        chat = await db.get(m.ChatSession, alice.session_id)
        assert chat is not None
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=chat.agent_id,
            source="chat",
            context={"chat_session_id": str(alice.session_id)},
        )
        db.add(run)
        await db.flush()
        run_id = run.id
        db.add(
            m.ChatMessage(
                tenant_id=tenant, session_id=alice.session_id, role="user", content="hello"
            )
        )

    async def readers(_db: AsyncSession, session: m.ChatSession) -> list[uuid.UUID]:
        return [bob.member_id] if session.id == alice.session_id else []

    shared = create_app(
        runtime_composition=RuntimeComposition(NOOP_SUPERVISION_RUN_HOOK, extra_readers=readers)
    )
    plain = create_app()
    sid = alice.session_id
    for app, readable in ((shared, True), (plain, False)):
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                h = _headers(tenant, "bob")
                msgs = await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=h)
                detail = await c.get(f"/api/v1/chat/sessions/{sid}", headers=h)
                run_get = await c.get(f"/api/v1/chat/sessions/{sid}/runs/{run_id}", headers=h)
                assert run_get.status_code == (200 if readable else 404), run_get.text
                if readable:
                    assert msgs.status_code == 200
                    assert [x["content"] for x in msgs.json()] == ["hello"]
                    assert detail.status_code == 200 and detail.json()["readOnly"] is True
                else:
                    assert msgs.status_code == 404 and detail.status_code == 404
                # Writes stay owner-only either way.
                for method, path, body in (
                    ("POST", f"/chat/sessions/{sid}/messages", {"message": "hi"}),
                    ("PATCH", f"/chat/sessions/{sid}", {"title": "x"}),
                    ("DELETE", f"/chat/sessions/{sid}", None),
                ):
                    r = await c.request(method, f"/api/v1{path}", json=body, headers=h)
                    assert r.status_code in (404, 405), (method, path, r.status_code)
                own = await c.get(f"/api/v1/chat/sessions/{sid}", headers=_headers(tenant, "alice"))
                assert own.json()["readOnly"] is False


async def test_reader_gets_one_redacted_channel(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    secret_object = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        bob = await copilot_seat(db, tenant, "bob")
        chat = await db.get(m.ChatSession, alice.session_id)
        assert chat is not None
        chat.pinned_refs = [{"kind": "data_source", "id": str(secret_object), "label": "Payroll"}]
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=chat.agent_id,
            source="chat",
            state="waiting_for_input",
            context={
                "chat_session_id": str(chat.id),
                "output": "the answer",
                "steps": 2,
                "toolCalls": [
                    {
                        "tool": "search",
                        "state": "done",
                        "step": 1,
                        "arguments": {"q": "salary of bob"},
                        "result": "secret result",
                    }
                ],
                "pending_question": "What is the password?",
                "todos": [{"content": "x", "status": "pending"}],
                "rendered_components": [{"component_key": "table", "props": {}}],
            },
        )
        db.add(run)
        await db.flush()
        db.add(
            m.Clarification(
                tenant_id=tenant,
                run_id=run.id,
                agent_id=chat.agent_id,
                question="What is the password?",
                status="open",
            )
        )
        msg = m.ChatMessage(
            tenant_id=tenant,
            session_id=chat.id,
            role="user",
            content="see this",
            context_refs=[{"kind": "data_source", "id": str(secret_object), "label": "Payroll"}],
        )
        db.add(msg)
        await db.flush()
        db.add(
            m.FileAttachment(
                tenant_id=tenant,
                owner_type="chat_message",
                owner_id=msg.id,
                bucket_key=f"k/{uuid.uuid4()}",
                filename="payslip.pdf",
                content_type="application/pdf",
                size_bytes=1234,
            )
        )
        run_id, sid = run.id, chat.id

    async def readers(_db: AsyncSession, session: m.ChatSession) -> list[uuid.UUID]:
        return [bob.member_id] if session.id == sid else []

    app = create_app(
        runtime_composition=RuntimeComposition(NOOP_SUPERVISION_RUN_HOOK, extra_readers=readers)
    )
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _headers(tenant, "bob")
            run_json = (await c.get(f"/api/v1/chat/sessions/{sid}/runs/{run_id}", headers=h)).json()
            detail = (await c.get(f"/api/v1/chat/sessions/{sid}", headers=h)).json()
            msgs = (await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=h)).json()
            owner_run = (
                await c.get(
                    f"/api/v1/chat/sessions/{sid}/runs/{run_id}", headers=_headers(tenant, "alice")
                )
            ).json()
            owner_msgs = (
                await c.get(
                    f"/api/v1/chat/sessions/{sid}/messages", headers=_headers(tenant, "alice")
                )
            ).json()
    assert run_json["output"] == "the answer" and run_json["steps"] == 2
    assert run_json["toolCalls"] == [{"tool": "search", "state": "done", "step": 1}]
    assert run_json["question"] is None and run_json["todos"] == []
    assert run_json["renderedComponents"] == []
    assert "salary" not in str(run_json) and "secret result" not in str(run_json)
    assert owner_run["toolCalls"][0]["arguments"] == {"q": "salary of bob"}
    assert owner_run["question"] == "What is the password?"

    assert detail["readOnly"] is True and detail["pendingQuestion"] is None
    assert [p["label"] for p in detail["pinnedRefs"]] == [""]
    assert "Payroll" not in str(detail)

    assert msgs[0]["contextRefs"][0]["label"] == ""
    assert msgs[0]["attachments"][0]["filename"] == "Anhang"
    assert msgs[0]["attachments"][0]["sizeBytes"] == 0
    assert "payslip" not in str(msgs) and "Payroll" not in str(msgs)
    assert owner_msgs[0]["attachments"][0]["filename"] == "payslip.pdf"


async def test_a_failing_extension_fails_closed_to_owner_only(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        alice = await copilot_seat(db, tenant, "alice")
        bob = await copilot_seat(db, tenant, "bob")
        db.add(
            m.ChatMessage(
                tenant_id=tenant, session_id=alice.session_id, role="user", content="hello"
            )
        )

    async def boom(_db: AsyncSession, _session: m.ChatSession) -> list[uuid.UUID]:
        raise RuntimeError("extension is down")

    app = create_app(
        runtime_composition=RuntimeComposition(NOOP_SUPERVISION_RUN_HOOK, extra_readers=boom)
    )
    sid = alice.session_id
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            for path in ("", "/messages"):
                r = await c.get(
                    f"/api/v1/chat/sessions/{sid}{path}", headers=_headers(tenant, "bob")
                )
                assert r.status_code == 404, (path, r.status_code)
            own = await c.get(
                f"/api/v1/chat/sessions/{sid}/messages", headers=_headers(tenant, "alice")
            )
            assert own.status_code == 200
    del bob
