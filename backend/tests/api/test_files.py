"""File attachment endpoints: upload (chat-session-scoped), download and
delete (shared by id) -- see oc8.api.v1.files. Every read/delete goes through
`_owned_attachment`, whose `chat_message` branch has two shapes depending on
whether the message has been sent yet (owner_id is a ChatSession.id before
send, a ChatMessage.id after chat/service.py's send_message re-points it) --
both are exercised explicitly below, not just the pre-send shape the upload
tests already touch incidentally."""

from __future__ import annotations

import io
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.knowledge.ingest import MAX_DOCUMENT_LENGTH
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str = "op") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _seed_agent_and_session(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Helpdesk", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Lennart")
        db.add(agent)
        await db.flush()
        agent_id = agent.id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            create_r = await c.post(
                "/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=_headers(tenant)
            )
            return agent_id, uuid.UUID(create_r.json()["id"])


async def test_upload_attachment_and_extract_text(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("notes.txt", io.BytesIO(b"important notes"), "text/plain")},
                headers=_headers(tenant),
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["filename"] == "notes.txt"
            attachment_id = body["id"]

            get_r = await c.get(f"/api/v1/files/{attachment_id}", headers=_headers(tenant))
            assert get_r.status_code == 200, get_r.text
            assert get_r.content == b"important notes"


async def test_upload_rejects_disallowed_content_type(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("virus.exe", io.BytesIO(b"MZ"), "application/x-msdownload")},
                headers=_headers(tenant),
            )
            assert r.status_code == 422, r.text


async def test_upload_rejects_oversized_file(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            oversized = b"x" * (26 * 1024 * 1024)
            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("big.txt", io.BytesIO(oversized), "text/plain")},
                headers=_headers(tenant),
            )
            assert r.status_code == 413, r.text


async def test_get_file_refuses_a_foreign_tenant(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("notes.txt", io.BytesIO(b"secret"), "text/plain")},
                headers=_headers(tenant),
            )
            attachment_id = r.json()["id"]

            # Different tenant entirely -- must 404, not just a different
            # subject within the same tenant (that ownership case is covered
            # by test_send_message_to_a_foreign_session_is_refused already).
            foreign_tenant = uuid.uuid4()
            foreign_token = get_identity_provider().mint(
                tenant_id=foreign_tenant, subject="op", role="org_admin"
            )
            get_r = await c.get(
                f"/api/v1/files/{attachment_id}",
                headers={"Authorization": f"Bearer {foreign_token}"},
            )
            assert get_r.status_code == 404, get_r.text


async def test_get_file_refuses_a_different_member_of_the_same_tenant_pre_send(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    """Pre-send: `FileAttachment.owner_id` is still the `ChatSession.id` (the
    upload happened before the message was ever sent), so `_owned_attachment`
    resolves ownership via `get_session` directly, same as `_owned_session`."""
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("notes.txt", io.BytesIO(b"secret"), "text/plain")},
                headers=_headers(tenant),
            )
            attachment_id = r.json()["id"]

            # Owning member can still read it.
            own_r = await c.get(f"/api/v1/files/{attachment_id}", headers=_headers(tenant))
            assert own_r.status_code == 200, own_r.text

            # A different member of the SAME tenant must not.
            other_r = await c.get(
                f"/api/v1/files/{attachment_id}", headers=_headers(tenant, subject="other-op")
            )
            assert other_r.status_code == 404, other_r.text


async def test_get_file_refuses_a_different_member_of_the_same_tenant_post_send(
    app_session: AppSessionFactory, minio_url: str, redis_url: str
) -> None:
    """Post-send: sending the chat message re-points `owner_id` from the
    `ChatSession.id` to the newly created `ChatMessage.id`
    (chat/service.py's send_message). `_owned_attachment` must fall back to
    resolving ownership through that message's session once the direct
    session lookup on `owner_id` comes back empty."""
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            upload_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("notes.txt", io.BytesIO(b"secret"), "text/plain")},
                headers=_headers(tenant),
            )
            attachment_id = upload_r.json()["id"]

            send_r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "see attached", "attachmentIds": [attachment_id]},
                headers=_headers(tenant),
            )
            assert send_r.status_code == 201, send_r.text

    async with app_session(tenant) as db:
        row = (
            await db.execute(
                select(m.FileAttachment).where(m.FileAttachment.id == uuid.UUID(attachment_id))
            )
        ).scalar_one()
        assert row.owner_type == "chat_message"
        assert str(row.owner_id) != str(session_id)  # re-pointed to the ChatMessage

    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            own_r = await c.get(f"/api/v1/files/{attachment_id}", headers=_headers(tenant))
            assert own_r.status_code == 200, own_r.text
            assert own_r.content == b"secret"

            other_r = await c.get(
                f"/api/v1/files/{attachment_id}", headers=_headers(tenant, subject="other-op")
            )
            assert other_r.status_code == 404, other_r.text


async def test_delete_file_removes_the_row_and_the_object(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("notes.txt", io.BytesIO(b"gone soon"), "text/plain")},
                headers=_headers(tenant),
            )
            attachment_id = r.json()["id"]

            del_r = await c.delete(f"/api/v1/files/{attachment_id}", headers=_headers(tenant))
            assert del_r.status_code == 204, del_r.text

            get_r = await c.get(f"/api/v1/files/{attachment_id}", headers=_headers(tenant))
            assert get_r.status_code == 404, get_r.text

    async with app_session(tenant) as db:
        row = (
            await db.execute(
                select(m.FileAttachment).where(m.FileAttachment.id == uuid.UUID(attachment_id))
            )
        ).scalar_one_or_none()
        assert row is None


async def test_download_forces_a_download_and_blocks_mime_sniffing(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    """`content_type` is the CLIENT-declared MIME type and `text/html` is on
    the allowlist, while Caddy serves the API and the SSR frontend from ONE
    origin -- so stored bytes must never be renderable as a same-origin
    document."""
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={
                    "file": (
                        "note.html",
                        io.BytesIO(b"<script>alert(1)</script>"),
                        "text/html",
                    )
                },
                headers=_headers(tenant),
            )
            attachment_id = r.json()["id"]

            get_r = await c.get(f"/api/v1/files/{attachment_id}", headers=_headers(tenant))
            assert get_r.status_code == 200, get_r.text
            assert get_r.headers["content-disposition"].startswith("attachment")
            assert get_r.headers["x-content-type-options"] == "nosniff"


async def test_get_and_delete_agent_instruction_file_for_a_tenant_wide_actor(
    app_session: AppSessionFactory, minio_url: str
) -> None:
    """`_owned_attachment`'s `agent_instructions` branch must compute
    `tenant_wide` the same way every sibling agent-scoped endpoint does
    (`GET /agents/{id}`, `GET /agents/{id}/instruction-files`) -- an
    org_admin who holds AGENT:VIEW tenant-wide but has no department
    membership must not 404 on a file the list endpoint just showed them.
    A hardcoded `tenant_wide=False` here previously made GET/DELETE
    `/files/{id}` 404 for exactly that actor while the list still worked."""
    tenant = uuid.uuid4()
    agent_id, _session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            upload_r = await c.post(
                f"/api/v1/agents/{agent_id}/instruction-files",
                files={"file": ("readme.txt", io.BytesIO(b"reference material"), "text/plain")},
                headers=_headers(tenant),
            )
            assert upload_r.status_code == 201, upload_r.text
            attachment_id = upload_r.json()["id"]

            get_r = await c.get(f"/api/v1/files/{attachment_id}", headers=_headers(tenant))
            assert get_r.status_code == 200, get_r.text
            assert get_r.content == b"reference material"

            del_r = await c.delete(f"/api/v1/files/{attachment_id}", headers=_headers(tenant))
            assert del_r.status_code == 204, del_r.text


async def test_upload_truncates_extracted_text_to_the_document_cap(
    app_session: AppSessionFactory, minio_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The design doc's Extraction Pipeline applies the SAME 200,000-char cap
    the KB ingest path uses -- truncating, never refusing. Without it a 25 MB
    text-extractable file's whole content is appended verbatim to the run's
    task text, and `trim_to_budget` always keeps the newest message."""
    queued: list[uuid.UUID] = []

    class _Queue:
        async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
            queued.append(run_id)

    # A file this large is also queued for indexing (spec §5.4); kept off the
    # shared stream, where another test's worker would pick it up.
    monkeypatch.setattr("oc8.knowledge.worker.get_ingestion_queue", lambda: _Queue())
    tenant = uuid.uuid4()
    _agent_id, session_id = await _seed_agent_and_session(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            oversized = b"a" * (MAX_DOCUMENT_LENGTH + 5_000)
            r = await c.post(
                f"/api/v1/chat/sessions/{session_id}/attachments",
                files={"file": ("long.txt", io.BytesIO(oversized), "text/plain")},
                headers=_headers(tenant),
            )
            assert r.status_code == 201, r.text  # truncated, NOT refused
            attachment_id = r.json()["id"]

    async with app_session(tenant) as db:
        row = await db.get(m.FileAttachment, uuid.UUID(attachment_id))
        assert row is not None
        assert row.extracted_text is not None
        assert len(row.extracted_text) == MAX_DOCUMENT_LENGTH
        assert row.size_bytes == len(oversized)  # the stored blob is untouched
        assert row.index_status == "queued" and len(queued) == 1
