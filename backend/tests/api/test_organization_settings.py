from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


@pytest.fixture
async def _client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client


async def test_organization_settings_are_readable_and_admin_editable(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        if await db.get(m.Organization, tenant) is None:
            db.add(m.Organization(id=tenant, slug=f"pilot-{uuid.uuid4().hex}", name="Pilot"))
            await db.flush()
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            before = await client.get("/api/v1/settings/organization", headers=headers)
            assert before.status_code == 200, before.text
            original = before.json()

            changed = await client.put(
                "/api/v1/settings/organization",
                json={"name": "Pilot Workspace", "region": "eu-central"},
                headers=headers,
            )
            assert changed.status_code == 200, changed.text
            assert changed.json() == {
                **original,
                "name": "Pilot Workspace",
                "region": "eu-central",
            }


async def _org_with_smtp_credential(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"mail-{uuid.uuid4().hex}", name="Mail"))
        await db.flush()
        credential = await create_credential(
            db,
            tenant_id=tenant,
            name="relay",
            credential_type="smtp_server",
            field_values={"host": "smtp.example.com", "port": "587", "from_address": "a@b.com"},
        )
        return credential.id


async def test_the_active_smtp_credential_round_trips(
    app_session: AppSessionFactory, _client: AsyncClient
) -> None:
    tenant = uuid.uuid4()
    credential_id = await _org_with_smtp_credential(app_session, tenant)
    headers = {"Authorization": f"Bearer {_token(tenant)}"}

    saved = await _client.put(
        "/api/v1/settings/organization",
        json={"name": "Mail", "region": "eu", "active_smtp_credential_id": str(credential_id)},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["active_smtp_credential_id"] == str(credential_id)

    read_back = await _client.get("/api/v1/settings/organization", headers=headers)
    assert read_back.json()["active_smtp_credential_id"] == str(credential_id)


async def test_an_unknown_smtp_credential_is_rejected(
    app_session: AppSessionFactory, _client: AsyncClient
) -> None:
    tenant = uuid.uuid4()
    await _org_with_smtp_credential(app_session, tenant)
    response = await _client.put(
        "/api/v1/settings/organization",
        json={"name": "Mail", "region": "eu", "active_smtp_credential_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {_token(tenant)}"},
    )
    assert response.status_code == 404, response.text


async def test_a_credential_of_another_type_is_rejected(
    app_session: AppSessionFactory, _client: AsyncClient
) -> None:
    """`list_credentials` filters on credential_type server-side, so an
    LLM-provider key can never be selected as the mail server."""
    tenant = uuid.uuid4()
    await _org_with_smtp_credential(app_session, tenant)
    async with app_session(tenant) as db:
        other = await create_credential(
            db,
            tenant_id=tenant,
            name="not-smtp",
            credential_type="anthropic_api_key",
            field_values={},
        )
        other_id = other.id
    response = await _client.put(
        "/api/v1/settings/organization",
        json={"name": "Mail", "region": "eu", "active_smtp_credential_id": str(other_id)},
        headers={"Authorization": f"Bearer {_token(tenant)}"},
    )
    assert response.status_code == 404, response.text


async def test_saving_only_name_and_region_keeps_the_mail_server(
    app_session: AppSessionFactory, _client: AsyncClient
) -> None:
    """The settings form posts {name, region} and nothing else
    (frontend/src/lib/hooks.ts). Renaming the org must not silently
    disconnect SMTP and break password reset."""
    tenant = uuid.uuid4()
    credential_id = await _org_with_smtp_credential(app_session, tenant)
    headers = {"Authorization": f"Bearer {_token(tenant)}"}
    await _client.put(
        "/api/v1/settings/organization",
        json={"name": "Mail", "region": "eu", "active_smtp_credential_id": str(credential_id)},
        headers=headers,
    )
    renamed = await _client.put(
        "/api/v1/settings/organization",
        json={"name": "Renamed", "region": "eu"},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["active_smtp_credential_id"] == str(credential_id)


async def test_an_explicit_null_clears_the_mail_server(
    app_session: AppSessionFactory, _client: AsyncClient
) -> None:
    tenant = uuid.uuid4()
    credential_id = await _org_with_smtp_credential(app_session, tenant)
    headers = {"Authorization": f"Bearer {_token(tenant)}"}
    await _client.put(
        "/api/v1/settings/organization",
        json={"name": "Mail", "region": "eu", "active_smtp_credential_id": str(credential_id)},
        headers=headers,
    )
    cleared = await _client.put(
        "/api/v1/settings/organization",
        json={"name": "Mail", "region": "eu", "active_smtp_credential_id": None},
        headers=headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["active_smtp_credential_id"] is None


async def test_non_admin_cannot_edit_organization_settings() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.put(
                "/api/v1/settings/organization",
                json={"name": "Nope", "region": "eu"},
                headers={"Authorization": f"Bearer {_token(tenant, 'member')}"},
            )
            assert response.status_code == 403, response.text


async def test_the_organisation_timezone_is_settable_and_audited(
    app_session: AppSessionFactory, _client: AsyncClient
) -> None:
    """New schedules default to it, so an admin must be able to set it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"tz-{uuid.uuid4().hex}", name="TZ"))
        await db.flush()
    headers = {"Authorization": f"Bearer {_token(tenant)}"}
    before = await _client.get("/api/v1/settings/organization", headers=headers)
    assert before.json()["timezone"] == "UTC"

    changed = await _client.put(
        "/api/v1/settings/organization",
        json={"name": "TZ", "region": "eu", "timezone": "Europe/Berlin"},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["timezone"] == "Europe/Berlin"
    me = await _client.get("/api/v1/me", headers=headers)
    assert me.json()["organisationTimezone"] == "Europe/Berlin"

    # Saving only name and region leaves it alone.
    kept = await _client.put(
        "/api/v1/settings/organization", json={"name": "TZ", "region": "eu"}, headers=headers
    )
    assert kept.json()["timezone"] == "Europe/Berlin"

    async with app_session(tenant) as db:
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "organization.settings.updated",
                    )
                )
            )
            .scalars()
            .all()
        )
        changes = [e.resource.get("timezone") for e in events if e.resource.get("timezone")]
        assert changes == [{"from": "UTC", "to": "Europe/Berlin"}]


async def test_an_unknown_organisation_timezone_is_refused(
    app_session: AppSessionFactory, _client: AsyncClient
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"tz-{uuid.uuid4().hex}", name="TZ"))
        await db.flush()
    headers = {"Authorization": f"Bearer {_token(tenant)}"}
    refused = await _client.put(
        "/api/v1/settings/organization",
        json={"name": "TZ", "region": "eu", "timezone": "Mars/Olympus"},
        headers=headers,
    )
    assert refused.status_code == 422
    assert "unknown time zone" in refused.text
    after = await _client.get("/api/v1/settings/organization", headers=headers)
    assert after.json()["timezone"] == "UTC"


async def test_non_admin_cannot_set_the_organisation_timezone(_client: AsyncClient) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    response = await _client.put(
        "/api/v1/settings/organization",
        json={"name": "Nope", "region": "eu", "timezone": "Europe/Berlin"},
        headers={"Authorization": f"Bearer {_token(tenant, 'member')}"},
    )
    assert response.status_code == 403, response.text
