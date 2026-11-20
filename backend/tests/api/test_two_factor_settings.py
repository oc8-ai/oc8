"""`GET/PUT /settings/organization/two-factor`: the policy, its three
refusals and its audit entry. Review focus 1 (lock-out) lives here."""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_URL = "/api/v1/settings/organization/two-factor"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _seed(
    app_session: AppSessionFactory,
    *,
    admin: bool = True,
    totp: bool = False,
    mail: bool = False,
    settings: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, dict[str, str]]:
    """An organisation and its caller; `admin` gives the caller the org_admin
    role. Without it the row has no role, so the token's org_admin claim still
    grants settings:manage while the caller is not an admin for the policy."""
    tenant = uuid.uuid4()
    subject = f"admin-{tenant.hex[:8]}@example.com"
    async with app_session(tenant) as db:
        db.add(
            m.Organization(id=tenant, slug=f"o-{tenant.hex[:8]}", name="O", settings=settings or {})
        )
        role = m.Role(tenant_id=tenant, name="org_admin", builtin=True)
        db.add(role)
        await db.flush()
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=uuid.uuid4(),
            email=subject,
            role_id=role.id if admin else None,
        )
        db.add(member)
        await db.flush()
        if totp:
            db.add(
                m.TotpCredential(
                    tenant_id=tenant,
                    member_id=member.id,
                    secret_ref=f"totp:{member.id}",
                    enrolled_at=dt.datetime.now(tz=dt.UTC),
                    backup_codes=[],
                )
            )
        if mail:
            credential = await create_credential(
                db,
                tenant_id=tenant,
                name="smtp",
                credential_type="smtp_server",
                field_values={"host": "smtp.example.com", "port": "587", "from_address": "a@b.c"},
            )
            org = await db.get(m.Organization, tenant)
            assert org is not None
            org.settings = {**org.settings, "active_smtp_credential_id": str(credential.id)}
    # org_admin claim: the token holds settings:manage; the ROW decides admin-ness.
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return tenant, {"Authorization": f"Bearer {token}"}


async def test_get_returns_the_default_when_nothing_is_stored(
    app_session: AppSessionFactory,
) -> None:
    _tenant, headers = await _seed(app_session)
    async with _http() as http:
        r = await http.get(_URL, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "requirement": "admins",
        "methods": ["totp", "email", "passkey"],
        "graceDays": 7,
        "emailAvailable": False,
        "passkeyAvailable": True,
    }


async def test_put_stores_the_policy_and_audits_from_and_to(
    app_session: AppSessionFactory,
) -> None:
    tenant, headers = await _seed(app_session, totp=True, mail=True)
    body = {"requirement": "everyone", "methods": ["totp", "email"], "graceDays": 3}
    async with _http() as http:
        r = await http.put(_URL, json=body, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {**body, "emailAvailable": True, "passkeyAvailable": True}
        again = await http.get(_URL, headers=headers)
    assert again.json()["requirement"] == "everyone"

    async with app_session(tenant) as db:
        org = await db.get(m.Organization, tenant)
        assert org is not None
        assert org.settings["two_factor"] == {
            "requirement": "everyone",
            "methods": ["totp", "email"],
            "grace_days": 3,
        }
        event = (
            await db.execute(
                select(m.AuditEvent).where(
                    m.AuditEvent.tenant_id == tenant,
                    m.AuditEvent.action == "organization.settings.updated",
                )
            )
        ).scalar_one()
    assert event.category == "admin"
    assert event.resource["two_factor"]["from"]["requirement"] == "admins"
    assert event.resource["two_factor"]["to"]["requirement"] == "everyone"


async def test_put_refuses_no_methods_unless_off(app_session: AppSessionFactory) -> None:
    _tenant, headers = await _seed(app_session, totp=True)
    async with _http() as http:
        r = await http.put(
            _URL, json={"requirement": "optional", "methods": [], "graceDays": 7}, headers=headers
        )
        assert r.status_code == 422, r.text
        ok = await http.put(
            _URL, json={"requirement": "off", "methods": [], "graceDays": 7}, headers=headers
        )
        assert ok.status_code == 200, ok.text


async def test_put_refuses_email_without_a_mail_server(app_session: AppSessionFactory) -> None:
    _tenant, headers = await _seed(app_session, totp=True)
    async with _http() as http:
        r = await http.put(
            _URL,
            json={"requirement": "optional", "methods": ["totp", "email"], "graceDays": 7},
            headers=headers,
        )
    assert r.status_code == 422, r.text
    assert "mail" in r.json()["detail"].lower()


@pytest.mark.parametrize("requirement", ["admins", "everyone"])
async def test_put_refuses_a_policy_the_saving_admin_cannot_satisfy(
    app_session: AppSessionFactory, requirement: str
) -> None:
    """Review focus 1: the admin saving a policy that requires them must hold
    an allowed method, so a save can never lock every admin out."""
    tenant, headers = await _seed(app_session, totp=False)
    async with _http() as http:
        r = await http.put(
            _URL,
            json={"requirement": requirement, "methods": ["totp"], "graceDays": 7},
            headers=headers,
        )
    assert r.status_code == 409, r.text
    async with app_session(tenant) as db:
        org = await db.get(m.Organization, tenant)
        assert org is not None
        assert "two_factor" not in org.settings


async def test_put_refuses_when_the_admins_only_method_is_dropped(
    app_session: AppSessionFactory,
) -> None:
    _tenant, headers = await _seed(app_session, totp=True, mail=True)
    async with _http() as http:
        r = await http.put(
            _URL,
            json={"requirement": "admins", "methods": ["email"], "graceDays": 7},
            headers=headers,
        )
    assert r.status_code == 409, r.text


async def test_a_non_admin_saving_admins_is_not_refused(app_session: AppSessionFactory) -> None:
    _tenant, headers = await _seed(app_session, admin=False, totp=False)
    async with _http() as http:
        r = await http.put(
            _URL,
            json={"requirement": "admins", "methods": ["totp"], "graceDays": 7},
            headers=headers,
        )
    assert r.status_code == 200, r.text


async def test_lowering_needs_no_method(app_session: AppSessionFactory) -> None:
    for requirement in ("off", "optional"):
        _tenant, headers = await _seed(app_session, totp=False)
        async with _http() as http:
            r = await http.put(
                _URL,
                json={"requirement": requirement, "methods": ["totp"], "graceDays": 7},
                headers=headers,
            )
        assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "body",
    [
        {"requirement": "sometimes", "methods": ["totp"], "graceDays": 7},
        {"requirement": "off", "methods": ["sms"], "graceDays": 7},
        {"requirement": "off", "methods": ["totp"], "graceDays": 31},
        {"requirement": "off", "methods": ["totp"], "graceDays": -1},
    ],
)
async def test_put_validates_the_body(app_session: AppSessionFactory, body: dict[str, Any]) -> None:
    _tenant, headers = await _seed(app_session, totp=True)
    async with _http() as http:
        r = await http.put(_URL, json=body, headers=headers)
    assert r.status_code == 422, r.text


async def test_change_needs_settings_manage(app_session: AppSessionFactory) -> None:
    tenant, _headers = await _seed(app_session)
    token = get_identity_provider().mint(
        tenant_id=tenant, subject="nobody@example.com", role="member"
    )
    async with _http() as http:
        r = await http.put(
            _URL,
            json={"requirement": "off", "methods": ["totp"], "graceDays": 7},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 403, r.text


# --- passkeys need a public URL a browser will create one for -------------------


async def test_get_reports_passkeys_unavailable_without_a_usable_public_url(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "frontend_base_url", "http://oc8.example.com")
    _tenant, headers = await _seed(app_session)
    async with _http() as http:
        r = await http.get(_URL, headers=headers)
    assert r.json()["passkeyAvailable"] is False


async def test_put_refuses_passkeys_while_they_are_unavailable(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "frontend_base_url", "")
    _tenant, headers = await _seed(app_session, totp=True)
    async with _http() as http:
        refused = await http.put(
            _URL,
            json={"requirement": "optional", "methods": ["totp", "passkey"], "graceDays": 7},
            headers=headers,
        )
        saved = await http.put(
            _URL,
            json={"requirement": "optional", "methods": ["totp"], "graceDays": 7},
            headers=headers,
        )
    assert refused.status_code == 422, refused.text
    assert "passkey" in refused.json()["detail"].lower()
    assert saved.status_code == 200, saved.text
