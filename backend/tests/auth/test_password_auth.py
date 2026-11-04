"""Tests for local password authentication (WP-B).

Covers:
- POST /auth/setup: initialize instance with first admin
- POST /auth/login: authenticate with email+password
- POST /auth/logout: no-op with JWT
- Password hashing and verification
- Security: no passwords in logs/exceptions
- Failure modes: missing org, already set up, wrong password, etc.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid

import pyotp
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from oc8 import models as m
from oc8.auth.password import hash_password, verify_password
from oc8.config import get_settings
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /auth/totp/confirm` calls `store_secret`, which needs a
    configured KEK to be available (mirrors tests/api/test_totp_endpoints.py
    and tests/api/test_mcp_logins.py) -- only exercised by this file's
    "requires a challenge" test, but harmless as an autouse for the rest."""
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


@pytest.fixture
async def app_with_db():
    """Create FastAPI app."""
    return create_app()


@pytest.fixture
async def client(app_with_db):
    """AsyncClient for the FastAPI app."""
    async with LifespanManager(app_with_db):
        transport = ASGITransport(app=app_with_db)
        async with AsyncClient(transport=transport, base_url="http://test") as _client:
            yield _client


@pytest.fixture
async def db_session(app_session: AppSessionFactory):
    """Database session bound to ACME_TENANT_ID."""
    async with app_session(ACME_TENANT_ID) as session:
        yield session


@pytest.fixture
async def org_in_db(app_session: AppSessionFactory) -> m.Organization:
    """Create a singleton Organization in the database.

    Commit, not flush: `client` drives the app over its OWN connection
    (auth/setup and auth/login open their own tenant_session, by design --
    see api/v1/auth.py), so a flush-only insert here is invisible to it.

    Uses its OWN independent session (via `app_session`, not the shared
    `db_session` fixture) so this commit's is_local app.tenant_id GUC dies on
    ITS connection only. `db_session` is a separate connection with its own
    GUC, still bound for the whole test -- so test bodies that add an
    OrgMember on `db_session` and commit still have a live GUC when they do.
    Sharing one session between this fixture and test bodies previously
    killed db_session's GUC here, before the test body's own member insert,
    causing an RLS violation on org_member.

    Committing means the row persists in the session-scoped test container
    beyond this one test, and `_get_singleton_organization` fails closed (409)
    on ANY second Organization row -- so this deletes the table's contents
    first, same pattern as tests/tenants/test_instance.py's own preflight
    tests.

    That cleanup runs on an owner-role (RLS-exempt) connection, not the
    app-role `app_session` used for the insert below: Organization rows left
    by OTHER test files (many exist across the suite, e.g.
    tests/tenants/test_provision.py's tenant-creation tests, each with their
    own random tenant id) are invisible to an app-role session bound to
    ACME_TENANT_ID -- RLS only lets it see/delete its own tenant's row. An
    app-role DELETE here would silently no-op on every other tenant's row,
    leaving `_get_singleton_organization`'s unbound, RLS-permitted
    discovery read to see more than one Organization and fail closed (409)
    on a test run order this file does not control.
    """
    owner_engine = create_async_engine(get_settings().migration_async_url, poolclass=NullPool)
    try:
        async with AsyncSession(owner_engine, expire_on_commit=False) as owner:
            await owner.execute(text("DELETE FROM org_member"))
            # Role rows aren't cleaned up by any other test in this file, and
            # this fixture now seeds one each call -- without deleting it
            # first, the second test in a full-file run hits the (tenant_id,
            # lower(name)) UNIQUE constraint re-seeding "org_admin".
            await owner.execute(text("DELETE FROM role"))
            await owner.execute(text("DELETE FROM organization"))
            await owner.commit()
    finally:
        await owner_engine.dispose()

    async with app_session(ACME_TENANT_ID) as session:
        org = m.Organization(id=ACME_TENANT_ID, slug="acme", name="ACME Corp")
        session.add(org)
        # A real tenant always gets this seeded by `create_tenant` (see
        # tenants/provision.py); this fixture creates the Organization
        # directly, bypassing that, so it must seed the one row
        # `password_setup` now depends on to persist the first admin's role.
        session.add(m.Role(tenant_id=ACME_TENANT_ID, name="org_admin", builtin=True))
    return org


# =============================================================================
# Password Hashing Tests (Unit)
# =============================================================================


def test_hash_password_valid() -> None:
    """Successfully hash a plaintext password."""
    plaintext = "MySecurePassword123!"
    hashed = hash_password(plaintext)
    assert hashed is not None
    assert len(hashed) > 0
    # Argon2 hashes start with $argon2id$ prefix
    assert hashed.startswith("$argon2id$")
    # Hash is not plaintext (obviously)
    assert plaintext not in hashed


def test_verify_password_correct() -> None:
    """Correct password verifies successfully."""
    plaintext = "MySecurePassword123!"
    hashed = hash_password(plaintext)
    assert verify_password(plaintext, hashed) is True


def test_verify_password_incorrect() -> None:
    """Wrong password fails verification."""
    plaintext = "MySecurePassword123!"
    hashed = hash_password(plaintext)
    assert verify_password("WrongPassword", hashed) is False


def test_verify_password_empty_plaintext() -> None:
    """Empty plaintext fails gracefully."""
    hashed = hash_password("ValidPassword123!")
    assert verify_password("", hashed) is False


def test_verify_password_empty_hash() -> None:
    """Empty hash fails gracefully."""
    assert verify_password("ValidPassword123!", "") is False


def test_verify_password_malformed_hash() -> None:
    """Malformed hash fails gracefully (not an exception)."""
    # An invalid/corrupted hash should return False, not raise
    assert verify_password("AnyPassword", "not-a-valid-hash") is False


def test_verify_password_case_sensitive() -> None:
    """Password verification is case-sensitive."""
    plaintext = "MyPassword"
    hashed = hash_password(plaintext)
    assert verify_password("MyPassword", hashed) is True
    assert verify_password("mypassword", hashed) is False
    assert verify_password("MYPASSWORD", hashed) is False


def test_hash_consistency() -> None:
    """Same password produces different hashes (salted)."""
    plaintext = "SamePassword"
    hash1 = hash_password(plaintext)
    hash2 = hash_password(plaintext)
    # Different hashes (salted)
    assert hash1 != hash2
    # Both verify correctly
    assert verify_password(plaintext, hash1) is True
    assert verify_password(plaintext, hash2) is True


# =============================================================================
# POST /auth/setup Tests
# =============================================================================


async def test_setup_bootstraps_a_single_community_instance_from_an_empty_database(
    client: AsyncClient,
) -> None:
    """A clean Community install creates its root and first local admin in one setup call."""
    owner_engine = create_async_engine(get_settings().migration_async_url, poolclass=NullPool)
    try:
        async with AsyncSession(owner_engine, expire_on_commit=False) as owner:
            await owner.execute(text("DELETE FROM org_member"))
            await owner.execute(text("DELETE FROM organization"))
            await owner.commit()
    finally:
        await owner_engine.dispose()

    response = await client.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": "SecurePassword123!",
            "displayName": "Admin User",
        },
    )

    assert response.status_code == 201, response.text
    owner_engine = create_async_engine(get_settings().migration_async_url, poolclass=NullPool)
    try:
        async with AsyncSession(owner_engine, expire_on_commit=False) as owner:
            organizations = (await owner.execute(select(m.Organization))).scalars().all()
            assert len(organizations) == 1
            assert organizations[0].slug == "oc8-community"
            assert organizations[0].name == "OC8 Community"
    finally:
        await owner_engine.dispose()


async def test_setup_creates_first_admin(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Setup creates org_member with admin role and password hash."""
    response = await client.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": "SecurePassword123!",
            "displayName": "Admin User",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert "token" in data
    assert "principal" in data
    assert "memberId" in data
    principal = data["principal"]
    assert principal["subject"] == "admin@example.com"
    assert principal["role"] == "org_admin"
    assert principal["kind"] == "operator"


async def test_setup_stores_the_sign_in_address_as_the_email(
    client: AsyncClient, app_session: AppSessionFactory, org_in_db: m.Organization
) -> None:
    """Local sign-in uses the address as the subject, so it is the email too."""
    response = await client.post(
        "/api/v1/auth/setup",
        json={"email": "first@example.com", "password": "SecurePassword123!"},
    )
    assert response.status_code == 201, response.text
    async with app_session(org_in_db.id) as db:
        member = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.subject == "first@example.com"))
        ).scalar_one()
    assert member.email == "first@example.com"


async def test_first_admin_keeps_admin_permissions_after_a_later_login(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    """The bug this pins: `/auth/setup`'s own minted token carries `role:
    "org_admin"`, but that claim is never persisted -- only `role_id` is
    read once the token that minted it expires (authz/authority.py: an
    unset `role_id` falls through to `password_login`'s own token, which
    always mints the empty `MEMBER_ROLE` by design). Before this fix, the
    very first admin permanently lost all authority on their very next
    login, with no other admin left to grant it back."""
    password = "SecurePassword123!"
    setup = await client.post(
        "/api/v1/auth/setup",
        json={"email": "admin@example.com", "password": password, "displayName": "Admin User"},
    )
    assert setup.status_code == 201, setup.text

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": password},
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]

    members_response = await client.get(
        "/api/v1/members", headers={"Authorization": f"Bearer {token}"}
    )
    assert members_response.status_code == 200, members_response.text


async def test_setup_requires_min_password_length(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    """Setup rejects passwords shorter than 8 characters (Pydantic validation)."""
    response = await client.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": "short",  # Less than 8 chars
            "displayName": "Admin User",
        },
    )
    # Pydantic validation error (422)
    assert response.status_code == 422


async def test_setup_rejects_when_members_exist(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Setup fails with 422 if any members already exist."""
    # Create one member first
    member = m.OrgMember(
        tenant_id=org_in_db.id,
        subject="existing@example.com",
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:existing@example.com"),
        display_name="Existing Member",
    )
    db_session.add(member)
    await db_session.commit()

    # Try to setup
    response = await client.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": "SecurePassword123!",
            "displayName": "Admin User",
        },
    )
    assert response.status_code == 422
    assert "already initialized" in response.json().get("detail", "").lower()


async def test_setup_password_not_in_response(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    """Password never appears in response."""
    password = "SecurePassword123!"
    response = await client.post(
        "/api/v1/auth/setup",
        json={
            "email": "admin@example.com",
            "password": password,
            "displayName": "Admin User",
        },
    )
    assert response.status_code == 201
    # Check that the plaintext password is not in response
    assert password not in response.text


# =============================================================================
# POST /auth/login Tests
# =============================================================================


async def test_login_with_correct_password(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Login with correct email and password returns token."""
    # Create a member with password_hash
    password = "CorrectPassword123!"
    hashed = hash_password(password)
    member = m.OrgMember(
        tenant_id=org_in_db.id,
        subject="user@example.com",
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:user@example.com"),
        display_name="Test User",
        password_hash=hashed,
    )
    db_session.add(member)
    await db_session.commit()

    # Login
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": password},
    )
    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert "principal" in data
    principal = data["principal"]
    assert principal["subject"] == "user@example.com"


async def test_login_without_assigned_role_gets_no_permissions(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """A member with no explicit `role_id` used to get an "org_admin" token
    role -- all 52 permissions -- from login alone. The token's role claim
    only matters when `role_id IS NULL` (authz/authority.py), so this member
    genuinely has nothing granted: the honest fail-closed floor, not a silent
    admin grant nobody configured."""
    password = "CorrectPassword123!"
    member = m.OrgMember(
        tenant_id=org_in_db.id,
        subject="unassigned@example.com",
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:unassigned@example.com"),
        display_name="Unassigned User",
        password_hash=hash_password(password),
    )
    db_session.add(member)
    await db_session.commit()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "unassigned@example.com", "password": password},
    )
    assert response.status_code == 200
    token = response.json()["token"]

    members_response = await client.get(
        "/api/v1/members", headers={"Authorization": f"Bearer {token}"}
    )
    assert members_response.status_code == 403


async def test_login_with_assigned_role_is_unaffected(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """The token's role claim change is invisible to a member who has an
    explicit `role_id` -- that row is an OVERRIDE, not a union term, so it
    fully decides their permissions regardless of what login mints."""
    password = "CorrectPassword123!"
    # `org_in_db` already seeds this tenant's builtin "org_admin" Role --
    # the Role.name unique-per-tenant constraint refuses a second one.
    role_id = (
        await db_session.execute(
            select(m.Role.id).where(m.Role.tenant_id == org_in_db.id, m.Role.name == "org_admin")
        )
    ).scalar_one()
    member = m.OrgMember(
        tenant_id=org_in_db.id,
        subject="admin-assigned@example.com",
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:admin-assigned@example.com"),
        display_name="Assigned Admin",
        password_hash=hash_password(password),
        role_id=role_id,
    )
    db_session.add(member)
    await db_session.commit()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin-assigned@example.com", "password": password},
    )
    assert response.status_code == 200
    token = response.json()["token"]

    members_response = await client.get(
        "/api/v1/members", headers={"Authorization": f"Bearer {token}"}
    )
    assert members_response.status_code == 200


async def test_login_with_wrong_password(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Login with wrong password returns 401."""
    password = "CorrectPassword123!"
    hashed = hash_password(password)
    member = m.OrgMember(
        tenant_id=org_in_db.id,
        subject="user@example.com",
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:user@example.com"),
        display_name="Test User",
        password_hash=hashed,
    )
    db_session.add(member)
    await db_session.commit()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": "WrongPassword"},
    )
    assert response.status_code == 401
    assert "Invalid email or password" in response.json().get("detail", "")


async def test_login_email_not_found(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    """Login with non-existent email returns 401."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nonexistent@example.com", "password": "AnyPassword123!"},
    )
    assert response.status_code == 401


async def test_login_no_password_hash_returns_401(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Login fails if member has no password_hash (e.g. minted from a
    dev-login token, or invited but never given a password)."""
    # Create member WITHOUT password_hash
    member = m.OrgMember(
        tenant_id=org_in_db.id,
        subject="no-password-user@example.com",
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:no-password-user@example.com"),
        display_name="No Password User",
        password_hash=None,  # No local password
    )
    db_session.add(member)
    await db_session.commit()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "no-password-user@example.com", "password": "AnyPassword123!"},
    )
    assert response.status_code == 401


async def test_login_soft_deleted_member(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Login fails for soft-deleted members."""
    password = "CorrectPassword123!"
    hashed = hash_password(password)
    member = m.OrgMember(
        tenant_id=org_in_db.id,
        subject="deleted@example.com",
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:deleted@example.com"),
        display_name="Deleted User",
        password_hash=hashed,
        deleted_at=dt.datetime.now(tz=dt.UTC),
    )
    db_session.add(member)
    await db_session.commit()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "deleted@example.com", "password": password},
    )
    assert response.status_code == 401


async def test_login_password_not_in_error_message(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Failed login error message does not contain password."""
    password = "CorrectPassword123!"
    wrong_password = "WrongPassword"
    hashed = hash_password(password)
    member = m.OrgMember(
        tenant_id=org_in_db.id,
        subject="user@example.com",
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:user@example.com"),
        display_name="Test User",
        password_hash=hashed,
    )
    db_session.add(member)
    await db_session.commit()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": wrong_password},
    )
    assert response.status_code == 401
    # Ensure password does not leak in error
    assert wrong_password not in response.json().get("detail", "")
    assert password not in response.json().get("detail", "")


# =============================================================================
# POST /auth/logout Tests
# =============================================================================


async def test_logout_always_succeeds(client: AsyncClient) -> None:
    """Logout always returns 204 (stateless JWT)."""
    response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 204


async def test_logout_requires_no_token(client: AsyncClient) -> None:
    """Logout works without a token (unguarded endpoint)."""
    # Should be callable without Authorization header
    response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 204


# =============================================================================
# /auth/config reports whether setup already ran
# =============================================================================


async def test_auth_config_reports_uninitialised_before_setup(
    client: AsyncClient,
    org_in_db: m.Organization,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An organization with no members has not been set up yet."""
    monkeypatch.setattr(
        "oc8.api.v1.auth.get_settings",
        lambda: get_settings().model_copy(update={"env": "production"}),
    )
    body = (await client.get("/api/v1/auth/config")).json()
    assert body["mode"] == "community"
    assert body["initialized"] is False


async def test_auth_config_reports_initialised_once_an_admin_exists(
    client: AsyncClient,
    db_session: AsyncSession,
    org_in_db: m.Organization,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this pins: a session that expired mid-wizard sent the operator
    to a Create-Admin form for an account that already existed, and the only
    way to find out was the 422 after filling it in. `/auth/config` must say
    so up front, using the same predicate `POST /auth/setup` refuses on.
    """
    monkeypatch.setattr(
        "oc8.api.v1.auth.get_settings",
        lambda: get_settings().model_copy(update={"env": "production"}),
    )
    db_session.add(
        m.OrgMember(
            tenant_id=org_in_db.id,
            subject="admin@oc8.ai",
            subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:admin@oc8.ai"),
            display_name="Admin",
            password_hash=hash_password("correct horse battery"),
            all_departments=True,
        )
    )
    await db_session.commit()

    body = (await client.get("/api/v1/auth/config")).json()
    assert body["initialized"] is True

    # And the two agree: setup really would refuse now.
    refused = await client.post(
        "/api/v1/auth/setup",
        json={"email": "admin@oc8.ai", "password": "another password", "displayName": "Admin"},
    )
    assert refused.status_code == 422, refused.text


async def test_setup_starts_the_grace_clock_for_the_bootstrap_admin(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    setup = await client.post(
        "/api/v1/auth/setup",
        json={
            "email": "grace-start@example.com",
            "password": "SecurePassword123!",
            "displayName": "A",
        },
    )
    assert setup.status_code == 201, setup.text

    member = (
        await db_session.execute(
            m.OrgMember.__table__.select().where(m.OrgMember.subject == "grace-start@example.com")
        )
    ).fetchone()
    assert member is not None
    assert member.totp_grace_started_at is not None


async def test_setup_response_itself_carries_the_grace_deadline(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    """The setup response is what the frontend's own nag banner reads (login.tsx
    handles /auth/setup's response with the exact same code path as /auth/login's),
    so `totpGraceExpiresAt` must be populated here, not only in the DB column the
    test above checks -- found live during Task 15's isolated-stack E2E walkthrough:
    the bootstrap admin's very first session showed no nag because this endpoint
    started the clock in the DB but never surfaced the deadline in its response."""
    setup = await client.post(
        "/api/v1/auth/setup",
        json={
            "email": "grace-in-setup-response@example.com",
            "password": "SecurePassword123!",
            "displayName": "A",
        },
    )
    assert setup.status_code == 201, setup.text
    body = setup.json()
    assert body["totpGraceExpiresAt"] is not None
    assert body.get("requiresTotpEnrollment", False) is False
    assert body.get("requiresTotpCode", False) is False


# =============================================================================
# POST /auth/login -- standalone 2FA three-outcome branch
# =============================================================================


async def test_login_with_no_totp_credential_and_grace_open_gets_a_full_session(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    """A freshly-promoted org_admin (grace just started) still logs in
    normally -- the grace period is a countdown, not an immediate block."""
    password = "SecurePassword123!"
    setup = await client.post(
        "/api/v1/auth/setup",
        json={"email": "grace-open@example.com", "password": password, "displayName": "A"},
    )
    assert setup.status_code == 201, setup.text

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "grace-open@example.com", "password": password},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["totpGraceExpiresAt"] is not None
    assert body.get("requiresTotpEnrollment", False) is False
    assert body.get("requiresTotpCode", False) is False


async def test_login_with_grace_expired_refuses_a_full_session(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    password = "SecurePassword123!"
    setup = await client.post(
        "/api/v1/auth/setup",
        json={"email": "grace-expired@example.com", "password": password, "displayName": "A"},
    )
    assert setup.status_code == 201, setup.text

    # Force the grace clock into the past, bypassing the API.
    member = (
        await db_session.execute(
            m.OrgMember.__table__.select().where(
                m.OrgMember.subject == "grace-expired@example.com"
            )
        )
    ).fetchone()
    await db_session.execute(
        m.OrgMember.__table__.update()
        .where(m.OrgMember.id == member.id)
        .values(totp_grace_started_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=30))
    )
    await db_session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "grace-expired@example.com", "password": password},
    )
    assert login.status_code == 200, login.text  # 200 with an enrollment token, not a 401
    body = login.json()
    assert body["requiresTotpEnrollment"] is True

    # The returned token is narrow -- proven by trying a real route with it.
    r = await client.get(
        "/api/v1/members", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert r.status_code == 403


async def test_login_one_second_past_the_grace_deadline_refuses_a_full_session(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Spec's own "Testing & Security" checklist: the boundary itself, not
    just "long after" (the existing 30-days-past test) -- `totp_gate`'s
    comparison is `now >= grace_deadline`, so a deadline exactly one second
    in the past must already refuse. A 1-second margin (rather than exactly
    0) is the smallest gap that survives real request latency between
    writing the timestamp here and the comparison running server-side."""
    password = "SecurePassword123!"
    setup = await client.post(
        "/api/v1/auth/setup",
        json={"email": "grace-boundary-past@example.com", "password": password, "displayName": "A"},
    )
    assert setup.status_code == 201, setup.text

    member = (
        await db_session.execute(
            m.OrgMember.__table__.select().where(
                m.OrgMember.subject == "grace-boundary-past@example.com"
            )
        )
    ).fetchone()
    # started_at + 7 days (TOTP_GRACE_DAYS) + 1 second = deadline already
    # 1 second in the past at the moment this write lands.
    await db_session.execute(
        m.OrgMember.__table__.update()
        .where(m.OrgMember.id == member.id)
        .values(
            totp_grace_started_at=dt.datetime.now(tz=dt.UTC)
            - dt.timedelta(days=7, seconds=1)
        )
    )
    await db_session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "grace-boundary-past@example.com", "password": password},
    )
    assert login.status_code == 200, login.text
    assert login.json()["requiresTotpEnrollment"] is True


async def test_login_a_few_seconds_before_the_grace_deadline_still_gets_a_full_session(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """The other side of the same boundary: a deadline still a few seconds
    in the FUTURE at write time must still grant a full session, not the
    existing "grace just started" test's wide-open case. The 5-second
    margin (rather than the spec's literal "one second before") is the
    smallest gap this test can use without risking the deadline passing
    during the request round-trip and flaking the assertion -- tight enough
    to prove the boundary is precise, not "long before"."""
    password = "SecurePassword123!"
    setup = await client.post(
        "/api/v1/auth/setup",
        json={"email": "grace-boundary-open@example.com", "password": password, "displayName": "A"},
    )
    assert setup.status_code == 201, setup.text

    member = (
        await db_session.execute(
            m.OrgMember.__table__.select().where(
                m.OrgMember.subject == "grace-boundary-open@example.com"
            )
        )
    ).fetchone()
    await db_session.execute(
        m.OrgMember.__table__.update()
        .where(m.OrgMember.id == member.id)
        .values(
            # deadline = started_at + 7 days = now + 5 seconds (still open)
            totp_grace_started_at=dt.datetime.now(tz=dt.UTC)
            - dt.timedelta(days=7)
            + dt.timedelta(seconds=5)
        )
    )
    await db_session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "grace-boundary-open@example.com", "password": password},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body.get("requiresTotpEnrollment", False) is False
    assert body["totpGraceExpiresAt"] is not None


async def test_login_with_an_enrolled_credential_requires_a_challenge(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    password = "SecurePassword123!"
    setup = await client.post(
        "/api/v1/auth/setup",
        json={"email": "challenged@example.com", "password": password, "displayName": "A"},
    )
    assert setup.status_code == 201, setup.text
    setup_token = setup.json()["token"]

    enroll = await client.post(
        "/api/v1/auth/totp/enroll", headers={"Authorization": f"Bearer {setup_token}"}
    )
    secret = enroll.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirm = await client.post(
        "/api/v1/auth/totp/confirm",
        json={"secret": secret, "code": code},
        headers={"Authorization": f"Bearer {setup_token}"},
    )
    assert confirm.status_code == 200, confirm.text

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "challenged@example.com", "password": password},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["requiresTotpCode"] is True
    r = await client.get(
        "/api/v1/members", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert r.status_code == 403


async def test_login_does_not_leak_totp_enrollment_status_before_password_is_verified(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    """A WRONG password must get the identical 401 shape regardless of
    whether the member has TOTP enrolled -- verified strictly BEFORE any
    TOTP branch is reached (mirrors this file's existing email-enumeration
    resistance property)."""
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "nonexistent-totp-check@example.com", "password": "wrong"},
    )
    assert r.status_code == 401
    assert set(r.json().keys()) == {"detail"}
