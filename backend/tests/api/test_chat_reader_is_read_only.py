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
