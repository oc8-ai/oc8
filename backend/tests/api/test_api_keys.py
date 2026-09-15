# backend/tests/api/test_api_keys.py
from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.auth.password import hash_password
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _client(app: object) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _member_with_session(
    app_session: AppSessionFactory, tenant: uuid.UUID, email: str
) -> str:
    async with app_session(tenant) as db:
        db.add(
            m.OrgMember(
                tenant_id=tenant,
                subject=email,
                subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:local:{email}"),
                password_hash=hash_password("Whatever123!"),
            )
        )
        await db.flush()
    return get_identity_provider().mint(tenant_id=tenant, subject=email, role="org_admin")


async def test_create_returns_the_plaintext_token_once(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    token = await _member_with_session(app_session, tenant, "create@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "Claude Desktop", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Claude Desktop"
    assert body["token"].startswith("oc8_ak_")
    assert body["tokenPrefix"] == body["token"][len("oc8_ak_") : len("oc8_ak_") + 8]
    assert body["enabled"] is True
    assert body["allowedOrigins"] == []


async def test_created_key_stores_only_the_hash_never_the_token(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    token = await _member_with_session(app_session, tenant, "hash-only@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "k", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token}"},
            )
    plaintext = r.json()["token"]

    async with app_session(tenant) as db:
        rows = (await db.execute(m.ApiKey.__table__.select())).fetchall()
        assert len(rows) == 1
        assert plaintext not in rows[0].token_hash
        assert rows[0].token_hash != plaintext


async def test_create_rejects_a_malformed_origin_with_422(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    token = await _member_with_session(app_session, tenant, "bad-origin@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "k", "allowedOrigins": ["not-a-url"]},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 422, r.text

    async with app_session(tenant) as db:
        rows = (await db.execute(m.ApiKey.__table__.select())).fetchall()
        assert rows == []


async def test_create_rejects_an_origin_with_a_path_with_422(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    token = await _member_with_session(app_session, tenant, "path-origin@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "k", "allowedOrigins": ["https://claude.ai/some/path"]},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 422, r.text


async def test_list_returns_only_the_caller_own_keys(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    token_a = await _member_with_session(app_session, tenant, "owner-a@example.com")
    token_b = await _member_with_session(app_session, tenant, "owner-b@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "A's key", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "B's key", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token_b}"},
            )

            list_a = await c.get(
                "/api/v1/settings/api-keys", headers={"Authorization": f"Bearer {token_a}"}
            )
            list_b = await c.get(
                "/api/v1/settings/api-keys", headers={"Authorization": f"Bearer {token_b}"}
            )
    assert [k["name"] for k in list_a.json()] == ["A's key"]
    assert [k["name"] for k in list_b.json()] == ["B's key"]


async def test_another_member_cannot_reach_or_toggle_or_delete_your_key(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    token_a = await _member_with_session(app_session, tenant, "victim@example.com")
    token_b = await _member_with_session(app_session, tenant, "attacker@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            created = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "victim's key", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            key_id = created.json()["id"]

            patch = await c.patch(
                f"/api/v1/settings/api-keys/{key_id}",
                json={"enabled": False},
                headers={"Authorization": f"Bearer {token_b}"},
            )
            delete = await c.delete(
                f"/api/v1/settings/api-keys/{key_id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
    assert patch.status_code == 404, patch.text
    assert delete.status_code == 404, delete.text

    async with app_session(tenant) as db:
        row = await db.get(m.ApiKey, uuid.UUID(key_id))
        assert row is not None
        assert row.enabled is True  # untouched by the other member's attempt


async def test_disable_then_enable_round_trip(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    token = await _member_with_session(app_session, tenant, "toggle@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            created = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "k", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token}"},
            )
            key_id = created.json()["id"]

            disabled = await c.patch(
                f"/api/v1/settings/api-keys/{key_id}",
                json={"enabled": False},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["enabled"] is False

            enabled = await c.patch(
                f"/api/v1/settings/api-keys/{key_id}",
                json={"enabled": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert enabled.json()["enabled"] is True


async def test_update_can_rename_and_change_allowed_origins(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    token = await _member_with_session(app_session, tenant, "rename@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            created = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "old name", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token}"},
            )
            key_id = created.json()["id"]

            updated = await c.patch(
                f"/api/v1/settings/api-keys/{key_id}",
                json={"name": "new name", "allowedOrigins": ["https://claude.ai"]},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "new name"
    assert updated.json()["allowedOrigins"] == ["https://claude.ai"]


async def test_create_and_delete_each_append_one_audit_event(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    token = await _member_with_session(app_session, tenant, "audited@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            created = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "audited key", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token}"},
            )
            key_id = created.json()["id"]

            await c.delete(
                f"/api/v1/settings/api-keys/{key_id}",
                headers={"Authorization": f"Bearer {token}"},
            )

    async with app_session(tenant) as db:
        rows = (
            await db.execute(m.AuditEvent.__table__.select().order_by(m.AuditEvent.seq))
        ).fetchall()
    events = [r for r in rows if r.category == "api_key"]
    assert [e.action for e in events] == ["api_key.created", "api_key.deleted"]
    assert events[0].resource["api_key_id"] == key_id
    assert events[0].resource["name"] == "audited key"
    assert events[1].resource["api_key_id"] == key_id
    assert events[1].resource["name"] == "audited key"


async def test_delete_then_a_second_delete_is_a_clean_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    token = await _member_with_session(app_session, tenant, "delete@example.com")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            created = await c.post(
                "/api/v1/settings/api-keys",
                json={"name": "k", "allowedOrigins": []},
                headers={"Authorization": f"Bearer {token}"},
            )
            key_id = created.json()["id"]

            first = await c.delete(
                f"/api/v1/settings/api-keys/{key_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            second = await c.delete(
                f"/api/v1/settings/api-keys/{key_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert first.status_code == 204, first.text
    assert second.status_code == 404, second.text

    async with app_session(tenant) as db:
        assert await db.get(m.ApiKey, uuid.UUID(key_id)) is None
