from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.credentials import registry
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_key_provider reads settings.secret_kek via get_settings(); set it so
    # the store is available (mirrors tests/api/test_secrets.py) -- creating a
    # credential with a password-kind field value stores it in the secret vault.
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture(autouse=True)
def _register_test_credential_type() -> Iterator[None]:
    registry.CORE_CREDENTIAL_TYPES["s3_api"] = CredentialTypeSpec(
        name="s3_api",
        display_name="S3 / Object storage",
        fields=[
            SetupFieldSpec(key="region", label="Region", kind="text", required=False),
            SetupFieldSpec(key="access_key", label="Access key", kind="password", required=False),
        ],
    )
    yield
    registry.CORE_CREDENTIAL_TYPES.pop("s3_api", None)


# A real, importable module-level target for `test_credential`'s core-type
# resolution branch (`importlib.import_module` + `getattr`) -- not a mock.
# Used by the `/credentials/{id}/test` endpoint tests below, which exercise
# `test_credential`'s field-gathering loop (the CredentialFieldNotSet
# skip-vs-raise branches, Task 15 Fix 2 gap) at the actual HTTP call site.
# Records the `values` it was called with so a test can assert an optional
# field left unset is genuinely omitted, not just present-with-None.
_field_gating_calls: list[dict[str, str]] = []


async def _validate_field_gating(values: dict[str, str]) -> None:
    _field_gating_calls.append(values)


@pytest.fixture()
def _register_field_gating_credential_type() -> Iterator[None]:
    _field_gating_calls.clear()
    registry.CORE_CREDENTIAL_TYPES["test_field_gating"] = CredentialTypeSpec(
        name="test_field_gating",
        display_name="Test: field gating",
        fields=[
            SetupFieldSpec(key="required_field", label="Required", kind="text", required=True),
            SetupFieldSpec(key="optional_field", label="Optional", kind="text", required=False),
        ],
        validate_entry_point="tests.api.test_credentials_endpoints:_validate_field_gating",
    )
    yield
    registry.CORE_CREDENTIAL_TYPES.pop("test_field_gating", None)


async def test_create_list_and_get_a_credential() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/credentials",
                json={
                    "name": "Prod S3",
                    "credentialType": "s3_api",
                    "fieldValues": {"region": "eu-central-1", "access_key": "AKIA_X"},
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["name"] == "Prod S3"
            assert "fieldValues" not in body or "access_key" not in body.get("fieldValues", {})
            cred_id = body["id"]

            r = await c.get("/api/v1/credentials?type=s3_api", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            assert [c["id"] for c in r.json()] == [cred_id]


async def test_delete_a_credential() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/credentials",
                json={"name": "Throwaway", "credentialType": "s3_api", "fieldValues": {}},
                headers=_headers(tenant),
            )
            cred_id = r.json()["id"]
            r = await c.delete(f"/api/v1/credentials/{cred_id}", headers=_headers(tenant))
            assert r.status_code == 204, r.text
            r = await c.get("/api/v1/credentials", headers=_headers(tenant))
            assert r.json() == []


async def test_credential_types_lists_registered_types() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/credential-types", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            names = [t["name"] for t in r.json()]
            assert "s3_api" in names


async def test_members_cannot_manage_credentials() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/credentials",
                json={"name": "x", "credentialType": "s3_api", "fieldValues": {}},
                headers=_headers(tenant, role="member"),
            )
            assert r.status_code == 403


@pytest.mark.usefixtures("_register_field_gating_credential_type")
async def test_test_credential_422s_when_a_required_field_is_unset(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/credentials",
                json={
                    "name": "Gated",
                    "credentialType": "test_field_gating",
                    "fieldValues": {},
                },
                headers=_headers(tenant),
            )
            # Saving it without the required field is refused, by name ...
            assert r.status_code == 422, r.text
            assert r.json()["detail"] == {
                "code": "credential_incomplete",
                "missing": ["required_field"],
            }
            # ... so only an older row can still lack it.
            async with app_session(tenant) as db:
                row = m.Credential(
                    tenant_id=tenant,
                    name="Gated",
                    credential_type="test_field_gating",
                    field_values={},
                    secret_refs={},
                )
                db.add(row)
                await db.flush()
                cred_id = str(row.id)

            r = await c.post(f"/api/v1/credentials/{cred_id}/test", headers=_headers(tenant))
            assert r.status_code == 422, r.text
            assert "required_field" in r.text
            # The required field's own CredentialFieldNotSet is fatal before
            # validate_entry_point is ever reached.
            assert _field_gating_calls == []


@pytest.mark.usefixtures("_register_field_gating_credential_type")
async def test_test_credential_succeeds_with_an_unset_optional_field_omitted() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/credentials",
                json={
                    "name": "Gated",
                    "credentialType": "test_field_gating",
                    "fieldValues": {"required_field": "present"},
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 201, r.text
            cred_id = r.json()["id"]

            r = await c.post(f"/api/v1/credentials/{cred_id}/test", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            assert r.json() == {"ok": True}
            # An unset OPTIONAL field is skipped, not raised, and never
            # reaches validate_entry_point's `values` at all -- distinct from
            # being present with an empty/None value.
            assert _field_gating_calls == [{"required_field": "present"}]


@pytest.mark.usefixtures("_register_field_gating_credential_type")
async def test_test_credential_passes_through_a_set_optional_field() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/credentials",
                json={
                    "name": "Gated",
                    "credentialType": "test_field_gating",
                    "fieldValues": {"required_field": "present", "optional_field": "also present"},
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 201, r.text
            cred_id = r.json()["id"]

            r = await c.post(f"/api/v1/credentials/{cred_id}/test", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            assert _field_gating_calls == [
                {"required_field": "present", "optional_field": "also present"}
            ]
