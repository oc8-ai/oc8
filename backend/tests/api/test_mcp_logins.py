from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.credentials import registry
from oc8.credentials.enablement import department_ids_by_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# tests/api/<this> -> tests -> backend -> repo root, where capas/ lives --
# needed for find_plugin to resolve the real odoo_mcp manifest and its
# credential_env_fields (same convention as test_mcp_endpoint.py).
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_key_provider reads settings.secret_kek via get_settings(); set it so
    # the store is available (mirrors tests/api/test_credentials_endpoints.py) --
    # creating a login's Credential with a password-kind field value stores it
    # in the secret vault.
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture(autouse=True)
def _register_test_credential_type() -> Iterator[None]:
    registry.CORE_CREDENTIAL_TYPES["odoo_login"] = CredentialTypeSpec(
        name="odoo_login",
        display_name="Odoo login",
        fields=[
            SetupFieldSpec(key="base_url", label="Base URL", kind="url", required=True),
            SetupFieldSpec(key="username", label="Username", kind="text", required=True),
            SetupFieldSpec(key="password", label="Password", kind="password", required=True),
        ],
    )
    registry.CORE_CREDENTIAL_TYPES["other_login"] = CredentialTypeSpec(
        name="other_login",
        display_name="Other login",
        fields=[SetupFieldSpec(key="token", label="Token", kind="password", required=True)],
    )
    yield
    registry.CORE_CREDENTIAL_TYPES.pop("odoo_login", None)
    registry.CORE_CREDENTIAL_TYPES.pop("other_login", None)


async def test_creating_a_login_creates_a_credential_and_no_connection(
    app_session: AppSessionFactory,
) -> None:
    # Unified connections "Storage": a login is one Credential; the tool keeps
    # its one connection row and no row is created per login.
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "Odoo (User 1)",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo.example.com",
                        "username": "user1",
                        "password": "secret1",
                    },
                    "scopes": {"read": ["list_customers"], "write": ["create_invoice"]},
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["name"] == "Odoo (User 1)"
            assert body["departmentId"] is None
            assert "password" not in str(body)
            assert "secret1" not in str(body)

            r = await c.get(
                "/api/v1/mcp/logins?credentialType=odoo_login", headers=_headers(tenant)
            )
            assert r.status_code == 200, r.text
            assert r.json() == []
    async with app_session(tenant) as db:
        cred = await db.get(m.Credential, uuid.UUID(body["credentialId"]))
        assert cred is not None and cred.name == "Odoo (User 1)"
        assert body["id"] == body["credentialId"]
        assert (await db.execute(m.McpConnection.__table__.select())).first() is None


async def test_creating_a_login_without_a_configured_kek_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Overrides the autouse `_kek` fixture's value for this test only (same
    # monkeypatch undo stack, mirrors tests/api/test_secrets.py's
    # test_create_secret_without_kek_returns_503) -- a password-kind field
    # routes through create_credential -> store_secret -> get_key_provider,
    # which raises SecretStoreUnavailable when secret_kek is unset.
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "secret_kek", "", raising=False)

    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "Odoo (User 1)",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo.example.com",
                        "username": "user1",
                        "password": "secret1",
                    },
                    "scopes": {"read": ["list_customers"], "write": ["create_invoice"]},
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 503, r.text


async def test_creating_a_login_reuses_an_existing_credential_instead_of_a_new_one() -> None:
    # An operator who already stored a credential -- e.g. via a capa's own
    # setup form -- should not have to retype the same values into "Create
    # login" a second time (live user report: the Odoo credential set up
    # through the capa's own setup form has no way to become a pinnable
    # login without this).
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            cred = await c.post(
                "/api/v1/credentials",
                json={
                    "name": "oc8-local",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo.example.com",
                        "username": "admin",
                        "password": "secret1",
                    },
                },
                headers=_headers(tenant),
            )
            assert cred.status_code == 201, cred.text
            cred_id = cred.json()["id"]

            login = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "odoo",
                    "credentialType": "odoo_login",
                    "credentialId": cred_id,
                    "scopes": [],
                },
                headers=_headers(tenant),
            )
            assert login.status_code == 201, login.text
            assert login.json()["name"] == "odoo"

            # No second credential was created -- exactly the one reused.
            creds = await c.get("/api/v1/credentials?type=odoo_login", headers=_headers(tenant))
            assert [row["id"] for row in creds.json()] == [cred_id]


async def test_creating_a_login_with_an_unknown_credential_id_is_404() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "odoo",
                    "credentialType": "odoo_login",
                    "credentialId": str(uuid.uuid4()),
                    "scopes": [],
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 404, r.text


async def test_creating_a_login_with_a_mismatched_credential_type_is_rejected() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            cred = await c.post(
                "/api/v1/credentials",
                json={
                    "name": "Some Token",
                    "credentialType": "other_login",
                    "fieldValues": {"token": "abc123"},
                },
                headers=_headers(tenant),
            )
            assert cred.status_code == 201, cred.text

            r = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "odoo",
                    "credentialType": "odoo_login",
                    "credentialId": cred.json()["id"],
                    "scopes": [],
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 400, r.text


async def test_creating_a_second_login_with_the_same_name_is_rejected() -> None:
    # A login's credential is named after the tool; credential names are
    # unique per organisation, so a second one under the same name conflicts.
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            first = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "Odoo Login",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo.example.com",
                        "username": "user1",
                        "password": "secret1",
                    },
                    "scopes": {},
                },
                headers=_headers(tenant),
            )
            assert first.status_code == 201, first.text
            first_id = first.json()["id"]

            dup = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "Odoo Login",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo2.example.com",
                        "username": "user2",
                        "password": "secret2",
                    },
                    "scopes": {},
                },
                headers=_headers(tenant),
            )
            assert dup.status_code == 409, dup.text
            assert first_id


async def test_two_departments_can_each_have_their_own_login_under_the_same_name(
    app_session: AppSessionFactory,
) -> None:
    # Live user report: "ich habe jetzt erstmal zwei verschiedene odoo
    # logins" -- one per department. Each login is named after its
    # department and enabled for it (unified connections "Storage").
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept_a = m.Department(tenant_id=tenant, name="Sales", frame={})
        dept_b = m.Department(tenant_id=tenant, name="Support", frame={})
        db.add_all([dept_a, dept_b])
        await db.flush()
        dept_a_id, dept_b_id = str(dept_a.id), str(dept_b.id)

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            first = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "odoo",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo.example.com",
                        "username": "user1",
                        "password": "secret1",
                    },
                    "scopes": {},
                    "departmentId": dept_a_id,
                },
                headers=_headers(tenant),
            )
            assert first.status_code == 201, first.text
            assert first.json()["departmentId"] == dept_a_id

            second = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "odoo",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo2.example.com",
                        "username": "user2",
                        "password": "secret2",
                    },
                    "scopes": {},
                    "departmentId": dept_b_id,
                },
                headers=_headers(tenant),
            )
            assert second.status_code == 201, second.text
            assert second.json()["departmentId"] == dept_b_id
    async with app_session(tenant) as db:
        enabled = await department_ids_by_credential(
            db, [uuid.UUID(first.json()["credentialId"]), uuid.UUID(second.json()["credentialId"])]
        )
        names = {c.name: c.id for c in (await db.execute(select(m.Credential))).scalars().all()}
    assert enabled == {
        names["odoo (Sales)"]: [uuid.UUID(dept_a_id)],
        names["odoo (Support)"]: [uuid.UUID(dept_b_id)],
    }


async def test_a_second_login_for_the_same_department_and_name_still_conflicts(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        dept_id = str(dept.id)

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            first = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "odoo",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo.example.com",
                        "username": "user1",
                        "password": "secret1",
                    },
                    "scopes": {},
                    "departmentId": dept_id,
                },
                headers=_headers(tenant),
            )
            assert first.status_code == 201, first.text

            dup = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "odoo",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo2.example.com",
                        "username": "user2",
                        "password": "secret2",
                    },
                    "scopes": {},
                    "departmentId": dept_id,
                },
                headers=_headers(tenant),
            )
            assert dup.status_code == 409, dup.text


async def test_creating_a_login_with_an_unknown_department_id_is_400() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/mcp/logins",
                json={
                    "name": "odoo",
                    "credentialType": "odoo_login",
                    "fieldValues": {
                        "base_url": "https://odoo.example.com",
                        "username": "user1",
                        "password": "secret1",
                    },
                    "scopes": {},
                    "departmentId": str(uuid.uuid4()),
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 400, r.text


async def test_choosing_another_login_for_a_department_replaces_the_old_one(
    app_session: AppSessionFactory,
) -> None:
    """`departmentId` names the department's one login: a second choice for
    the same department replaces the first there (not both enabled)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        dept_id = dept.id

    def _login(url: str) -> dict[str, object]:
        return {
            "name": "odoo",
            "credentialType": "odoo_login",
            "fieldValues": {"base_url": url, "username": "u", "password": "p"},
            "scopes": {},
        }

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            ids = []
            for name, url in (("A", "https://a.test"), ("B", "https://b.test")):
                r = await c.post(
                    "/api/v1/credentials",
                    json={**_login(url), "name": name},
                    headers=_headers(tenant),
                )
                assert r.status_code == 201, r.text
                ids.append(r.json()["id"])
            for cred_id in ids:
                r = await c.post(
                    "/api/v1/mcp/logins",
                    json={
                        **_login("unused"),
                        "credentialId": cred_id,
                        "departmentId": str(dept_id),
                    },
                    headers=_headers(tenant),
                )
                assert r.status_code == 201, r.text
    first, second = (uuid.UUID(i) for i in ids)
    async with app_session(tenant) as db:
        enabled = await department_ids_by_credential(db, [first, second])
    assert enabled == {first: [], second: [dept_id]}
