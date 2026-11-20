"""Passkeys (WebAuthn) as a second factor: registration and sign-in with a
software authenticator, everything that must refuse an assertion, the
pending-token reach, renaming, removal and an administrator's reset."""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update

from oc8 import models as m
from oc8.api.deps import deny_totp_pending_principals
from oc8.auth import get_identity_provider
from oc8.auth.password import hash_password
from oc8.authz.permissions import MEMBER_ROLE
from oc8.main import create_app
from tests.auth.soft_authenticator import SoftAuthenticator
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

PASSWORD = "Whatever123!"
_BASE = "/api/v1/auth/two-factor/passkey"
_REGISTER_OPTIONS = f"{_BASE}/register/options"
_REGISTER_VERIFY = f"{_BASE}/register/verify"
_AUTH_OPTIONS = f"{_BASE}/authenticate/options"
_AUTH_VERIFY = f"{_BASE}/authenticate/verify"
_METHODS = "/api/v1/auth/two-factor/methods"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )
    monkeypatch.setattr(config.get_settings(), "frontend_base_url", "http://localhost:8080")


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@dataclass
class Seeded:
    tenant: uuid.UUID
    member_id: uuid.UUID
    subject: str

    def headers(self, *scopes: str, role: str = MEMBER_ROLE) -> dict[str, str]:
        token = get_identity_provider().mint(
            tenant_id=self.tenant, subject=self.subject, role=role, scopes=list(scopes)
        )
        return {"Authorization": f"Bearer {token}"}


async def _seed(
    app_session: AppSessionFactory,
    *,
    policy: dict[str, Any] | None = None,
    admin: bool = False,
    tenant: uuid.UUID | None = None,
) -> Seeded:
    tenant = tenant or uuid.uuid4()
    subject = f"m-{uuid.uuid4().hex[:8]}@example.com"
    async with app_session(tenant) as db:
        if await db.get(m.Organization, tenant) is None:
            settings: dict[str, Any] = {}
            if policy is not None:
                settings["two_factor"] = policy
            db.add(
                m.Organization(id=tenant, slug=f"o-{tenant.hex[:8]}", name="O", settings=settings)
            )
        role_id = None
        if admin:
            role = (
                await db.execute(select(m.Role).where(m.Role.name == "org_admin"))
            ).scalar_one_or_none()
            if role is None:
                role = m.Role(tenant_id=tenant, name="org_admin", builtin=True)
                db.add(role)
                await db.flush()
            role_id = role.id
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=uuid.uuid4(),
            email=subject,
            password_hash=hash_password(PASSWORD),
            role_id=role_id,
            totp_grace_started_at=dt.datetime.now(tz=dt.UTC),
        )
        db.add(member)
        await db.flush()
        return Seeded(tenant=tenant, member_id=member.id, subject=subject)


async def _options(http: AsyncClient, path: str, headers: dict[str, str]) -> dict[str, Any]:
    r = await http.post(path, headers=headers)
    assert r.status_code == 200, r.text
    options: dict[str, Any] = r.json()["options"]
    return options


async def _register(
    http: AsyncClient,
    s: Seeded,
    key: SoftAuthenticator,
    *,
    label: str = "Laptop",
    headers: dict[str, str] | None = None,
) -> Response:
    headers = headers or s.headers()
    options = await _options(http, _REGISTER_OPTIONS, headers)
    return await http.post(
        _REGISTER_VERIFY,
        json={"credential": key.register(options), "label": label},
        headers=headers,
    )


async def _sign_in(http: AsyncClient, s: Seeded, key: SoftAuthenticator, **kw: Any) -> Response:
    headers = s.headers("totp:challenge")
    options = await _options(http, _AUTH_OPTIONS, headers)
    return await http.post(
        _AUTH_VERIFY, json={"credential": key.assert_(options, **kw)}, headers=headers
    )


async def _rows(app_session: AppSessionFactory, s: Seeded) -> list[m.SecondFactor]:
    async with app_session(s.tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.SecondFactor)
                    .where(m.SecondFactor.member_id == s.member_id)
                    .order_by(m.SecondFactor.created_at)
                )
            ).scalars()
        )


async def _events(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.AuditEvent]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action.like("two_factor.%"),
                    )
                )
            ).scalars()
        )


# --- registration and sign-in --------------------------------------------------


async def test_register_then_sign_in_with_the_passkey(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        options = await _options(http, _REGISTER_OPTIONS, s.headers())
        assert options["rp"]["id"] == "localhost"
        assert options["excludeCredentials"] == []
        r = await http.post(
            _REGISTER_VERIFY,
            json={"credential": key.register(options), "label": "Laptop"},
            headers=s.headers(),
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["backupCodes"]) == 10

        headers = s.headers("totp:challenge")
        options = await _options(http, _AUTH_OPTIONS, headers)
        assert [c["id"] for c in options["allowCredentials"]] == [key.id]
        r = await http.post(
            _AUTH_VERIFY, json={"credential": key.assert_(options)}, headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["memberId"] == str(s.member_id)
        assert body["principal"]["scopes"] == []

    [row] = await _rows(app_session, s)
    assert row.method == "passkey"
    assert row.label == "Laptop"
    assert row.data["credential_id"] == key.id
    assert row.data["sign_count"] == 1
    assert row.data["transports"] == ["internal"]
    assert row.data["public_key"]
    assert row.last_used_at is not None
    async with app_session(s.tenant) as db:
        member = await db.get(m.OrgMember, s.member_id)
        assert member is not None and member.totp_grace_started_at is None
    enrolled = [
        e for e in await _events(app_session, s.tenant) if e.action == "two_factor.enrolled"
    ]
    assert [e.resource for e in enrolled] == [{"member_id": str(s.member_id), "method": "passkey"}]


async def test_the_enrol_token_can_register_a_passkey(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        r = await _register(http, s, SoftAuthenticator(), headers=s.headers("totp:enroll"))
    assert r.status_code == 200, r.text
    assert len(await _rows(app_session, s)) == 1


async def test_a_member_can_hold_several_passkeys(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    first, second = SoftAuthenticator(), SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, first, label="Laptop")).status_code == 200
        r = await _register(http, s, second, label="Key")
        assert r.status_code == 200, r.text
        # Backup codes come with the first method only.
        assert r.json()["backupCodes"] == []
        options = await _options(http, _REGISTER_OPTIONS, s.headers())
        assert {c["id"] for c in options["excludeCredentials"]} == {first.id, second.id}
        assert (await _sign_in(http, s, second)).status_code == 200
        listed = (await http.get(_METHODS, headers=s.headers())).json()
    assert [x["method"] for x in listed["methods"]] == ["passkey"]
    assert [p["label"] for p in listed["passkeys"]] == ["Laptop", "Key"]
    assert listed["passkeys"][1]["lastUsedAt"] is not None


async def test_the_same_credential_cannot_be_registered_twice(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        r = await _register(http, s, key)
    assert r.status_code == 409, r.text
    assert len(await _rows(app_session, s)) == 1


async def test_registration_is_refused_when_the_policy_disallows_passkeys(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(
        app_session, policy={"requirement": "optional", "methods": ["totp"], "grace_days": 7}
    )
    async with _http() as http:
        r = await http.post(_REGISTER_OPTIONS, headers=s.headers())
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("label", ["", "   ", "x" * 65])
async def test_a_passkey_needs_a_sensible_name(app_session: AppSessionFactory, label: str) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        r = await _register(http, s, SoftAuthenticator(), label=label)
    assert r.status_code == 422, r.text
    assert await _rows(app_session, s) == []


# --- what an assertion or a registration must never get past -------------------


async def test_a_registration_from_another_origin_is_refused(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator(origin="http://evil.example")
    async with _http() as http:
        r = await _register(http, s, key)
    assert r.status_code == 400, r.text
    assert await _rows(app_session, s) == []


async def test_a_registration_for_another_relying_party_is_refused(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        options = await _options(http, _REGISTER_OPTIONS, s.headers())
        credential = SoftAuthenticator().register(options, rp_id="evil.example")
        r = await http.post(
            _REGISTER_VERIFY, json={"credential": credential, "label": "x"}, headers=s.headers()
        )
    assert r.status_code == 400, r.text
    assert await _rows(app_session, s) == []


async def test_an_assertion_from_another_origin_is_refused(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        r = await _sign_in(http, s, key, origin="http://evil.example")
    assert r.status_code == 401, r.text
    assert "token" not in r.json()


async def test_an_assertion_for_another_relying_party_is_refused(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        r = await _sign_in(http, s, key, rp_id="evil.example")
    assert r.status_code == 401, r.text


async def test_a_replayed_assertion_is_refused(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        assert (await _sign_in(http, s, key, sign_count=5)).status_code == 200
        same = await _sign_in(http, s, key, sign_count=5)
        lower = await _sign_in(http, s, key, sign_count=3)
        higher = await _sign_in(http, s, key, sign_count=6)
    assert same.status_code == 401, same.text
    assert lower.status_code == 401, lower.text
    assert higher.status_code == 200, higher.text
    [row] = await _rows(app_session, s)
    assert row.data["sign_count"] == 6


async def test_an_authenticator_that_never_counts_keeps_working(
    app_session: AppSessionFactory,
) -> None:
    """A sign count of zero means the authenticator does not count: nothing
    to compare, so it is accepted every time."""
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        assert (await _sign_in(http, s, key, sign_count=0)).status_code == 200
        assert (await _sign_in(http, s, key, sign_count=0)).status_code == 200


async def test_a_challenge_is_answered_once(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        headers = s.headers("totp:challenge")
        options = await _options(http, _AUTH_OPTIONS, headers)
        first = await http.post(
            _AUTH_VERIFY, json={"credential": key.assert_(options)}, headers=headers
        )
        again = await http.post(
            _AUTH_VERIFY, json={"credential": key.assert_(options)}, headers=headers
        )
    assert first.status_code == 200, first.text
    assert again.status_code == 401, again.text


async def test_a_failed_answer_spends_the_challenge(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        headers = s.headers("totp:challenge")
        options = await _options(http, _AUTH_OPTIONS, headers)
        bad = key.assert_(options, origin="http://evil.example")
        assert (
            await http.post(_AUTH_VERIFY, json={"credential": bad}, headers=headers)
        ).status_code == 401
        r = await http.post(
            _AUTH_VERIFY, json={"credential": key.assert_(options)}, headers=headers
        )
    assert r.status_code == 401, r.text


async def test_an_expired_challenge_is_refused(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        headers = s.headers("totp:challenge")
        options = await _options(http, _AUTH_OPTIONS, headers)
        async with app_session(s.tenant) as db:
            await db.execute(
                update(m.PasskeyChallenge)
                .where(m.PasskeyChallenge.member_id == s.member_id)
                .values(expires_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1))
            )
        r = await http.post(
            _AUTH_VERIFY, json={"credential": key.assert_(options)}, headers=headers
        )
    assert r.status_code == 401, r.text


async def test_a_newer_challenge_replaces_the_older_one(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        headers = s.headers("totp:challenge")
        older = await _options(http, _AUTH_OPTIONS, headers)
        await _options(http, _AUTH_OPTIONS, headers)
        r = await http.post(_AUTH_VERIFY, json={"credential": key.assert_(older)}, headers=headers)
    assert r.status_code == 401, r.text


async def test_a_registration_challenge_cannot_answer_a_sign_in(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        register_options = await _options(http, _REGISTER_OPTIONS, s.headers())
        headers = s.headers("totp:challenge")
        r = await http.post(
            _AUTH_VERIFY,
            json={"credential": key.assert_({"challenge": register_options["challenge"]})},
            headers=headers,
        )
    assert r.status_code == 401, r.text


async def test_one_members_passkey_cannot_answer_for_another(
    app_session: AppSessionFactory,
) -> None:
    alice = await _seed(app_session)
    bob = await _seed(app_session, tenant=alice.tenant)
    alice_key, bob_key = SoftAuthenticator(), SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, alice, alice_key)).status_code == 200
        assert (await _register(http, bob, bob_key)).status_code == 200
        headers = bob.headers("totp:challenge")
        options = await _options(http, _AUTH_OPTIONS, headers)
        assert alice_key.id not in [c["id"] for c in options["allowCredentials"]]
        r = await http.post(
            _AUTH_VERIFY, json={"credential": alice_key.assert_(options)}, headers=headers
        )
    assert r.status_code == 401, r.text
    assert "token" not in r.json()


async def test_a_malformed_credential_is_refused(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        assert (await _register(http, s, SoftAuthenticator())).status_code == 200
        headers = s.headers("totp:challenge")
        await _options(http, _AUTH_OPTIONS, headers)
        r = await http.post(_AUTH_VERIFY, json={"credential": {"id": "x"}}, headers=headers)
    assert r.status_code == 401, r.text


async def test_sign_in_options_need_a_passkey(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        r = await http.post(_AUTH_OPTIONS, headers=s.headers("totp:challenge"))
    assert r.status_code == 403, r.text


async def test_a_passkey_the_policy_no_longer_allows_cannot_sign_in(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        headers = s.headers("totp:challenge")
        options = await _options(http, _AUTH_OPTIONS, headers)
        async with app_session(s.tenant) as db:
            org = await db.get(m.Organization, s.tenant)
            assert org is not None
            org.settings = {
                "two_factor": {"requirement": "optional", "methods": ["totp"], "grace_days": 7}
            }
        r = await http.post(
            _AUTH_VERIFY, json={"credential": key.assert_(options)}, headers=headers
        )
    assert r.status_code == 401, r.text


# --- availability: a public URL that can carry a passkey ------------------------


async def test_passkeys_are_unavailable_without_a_usable_public_url(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import config

    s = await _seed(app_session)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        monkeypatch.setattr(config.get_settings(), "frontend_base_url", "http://oc8.example.com")
        assert (await http.post(_REGISTER_OPTIONS, headers=s.headers())).status_code == 403
        assert (
            await http.post(_AUTH_OPTIONS, headers=s.headers("totp:challenge"))
        ).status_code == 403
        listed = (await http.get(_METHODS, headers=s.headers())).json()
    assert "passkey" not in listed["enrollableMethods"]
    assert "passkey" not in listed["allowedMethods"]
    assert listed["methods"][0]["allowed"] is False


# --- rename and removal --------------------------------------------------------


async def test_a_passkey_can_be_renamed(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    other = await _seed(app_session, tenant=s.tenant)
    async with _http() as http:
        assert (await _register(http, s, SoftAuthenticator())).status_code == 200
        [row] = await _rows(app_session, s)
        r = await http.patch(f"{_BASE}/{row.id}", json={"label": " Work key "}, headers=s.headers())
        assert r.status_code == 200, r.text
        assert r.json()["label"] == "Work key"
        theirs = await http.patch(
            f"{_BASE}/{row.id}", json={"label": "Mine now"}, headers=other.headers()
        )
        assert theirs.status_code == 404, theirs.text
        empty = await http.patch(f"{_BASE}/{row.id}", json={"label": " "}, headers=s.headers())
        assert empty.status_code == 422, empty.text
    [row] = await _rows(app_session, s)
    assert row.label == "Work key"


async def _remove(http: AsyncClient, s: Seeded, factor_id: uuid.UUID, password: str) -> Response:
    return await http.request(
        "DELETE", f"{_BASE}/{factor_id}", json={"currentPassword": password}, headers=s.headers()
    )


async def test_removing_a_passkey_needs_the_password_and_is_audited(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        assert (await _register(http, s, SoftAuthenticator())).status_code == 200
        [row] = await _rows(app_session, s)
        assert (await _remove(http, s, row.id, "wrong-password")).status_code == 401
        r = await _remove(http, s, row.id, PASSWORD)
        assert r.status_code == 204, r.text
    assert await _rows(app_session, s) == []
    async with app_session(s.tenant) as db:
        # Nothing left: the backup codes went with the last method.
        assert (await db.execute(select(m.TotpCredential))).scalars().all() == []
    removed = [e for e in await _events(app_session, s.tenant) if e.action == "two_factor.removed"]
    assert [e.resource for e in removed] == [{"member_id": str(s.member_id), "method": "passkey"}]


async def test_the_last_passkey_is_kept_while_the_policy_requires_one(
    app_session: AppSessionFactory,
) -> None:
    policy = {"requirement": "everyone", "methods": ["passkey"], "grace_days": 7}
    s = await _seed(app_session, policy=policy)
    async with _http() as http:
        assert (await _register(http, s, SoftAuthenticator(), label="A")).status_code == 200
        assert (await _register(http, s, SoftAuthenticator(), label="B")).status_code == 200
        first, second = await _rows(app_session, s)
        listed = (await http.get(_METHODS, headers=s.headers())).json()
        assert [p["removable"] for p in listed["passkeys"]] == [True, True]
        assert (await _remove(http, s, first.id, PASSWORD)).status_code == 204
        listed = (await http.get(_METHODS, headers=s.headers())).json()
        assert [p["removable"] for p in listed["passkeys"]] == [False]
        r = await _remove(http, s, second.id, PASSWORD)
    assert r.status_code == 409, r.text
    assert [x.id for x in await _rows(app_session, s)] == [second.id]


async def test_another_members_passkey_cannot_be_removed(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    other = await _seed(app_session, tenant=s.tenant)
    async with _http() as http:
        assert (await _register(http, s, SoftAuthenticator())).status_code == 200
        [row] = await _rows(app_session, s)
        r = await _remove(http, other, row.id, PASSWORD)
    assert r.status_code == 404, r.text
    assert len(await _rows(app_session, s)) == 1


async def test_an_admin_reset_removes_passkeys_and_open_challenges(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    admin = await _seed(app_session, admin=True, tenant=s.tenant)
    key = SoftAuthenticator()
    async with _http() as http:
        assert (await _register(http, s, key)).status_code == 200
        headers = s.headers("totp:challenge")
        options = await _options(http, _AUTH_OPTIONS, headers)
        r = await http.post(
            f"/api/v1/members/{s.member_id}/two-factor/reset",
            headers=admin.headers(role="org_admin"),
        )
        assert r.status_code == 204, r.text
        r = await http.post(
            _AUTH_VERIFY, json={"credential": key.assert_(options)}, headers=headers
        )
    assert r.status_code == 401, r.text
    assert await _rows(app_session, s) == []
    async with app_session(s.tenant) as db:
        open_challenges = (
            (
                await db.execute(
                    select(m.PasskeyChallenge).where(
                        m.PasskeyChallenge.member_id == s.member_id,
                        m.PasskeyChallenge.used_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert open_challenges == []


# --- the pending-token reach ----------------------------------------------------


def _request_for(method: str, path: str, token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


_ANY_ID = uuid.uuid4()
_ROUTES = [
    ("POST", _REGISTER_OPTIONS, {"totp:enroll"}),
    ("POST", _REGISTER_VERIFY, {"totp:enroll"}),
    ("POST", _AUTH_OPTIONS, {"totp:challenge"}),
    ("POST", _AUTH_VERIFY, {"totp:challenge"}),
    ("PATCH", f"{_BASE}/{_ANY_ID}", set()),
    ("DELETE", f"{_BASE}/{_ANY_ID}", set()),
]


@pytest.mark.parametrize("scope", ["totp:enroll", "totp:challenge"])
@pytest.mark.parametrize(("method", "path", "reached_by"), _ROUTES)
async def test_a_pending_token_reaches_only_its_own_passkey_routes(
    scope: str, method: str, path: str, reached_by: set[str]
) -> None:
    token = get_identity_provider().mint(
        tenant_id=uuid.uuid4(), subject="p@example.com", role="org_admin", scopes=[scope]
    )
    request = _request_for(method, path, token)
    if scope in reached_by:
        await deny_totp_pending_principals(request)
        return
    with pytest.raises(HTTPException) as exc:
        await deny_totp_pending_principals(request)
    assert exc.value.status_code == 403
