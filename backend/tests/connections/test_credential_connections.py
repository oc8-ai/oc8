from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8.connections.checks import classify_check_error
from oc8.credentials.errors import CredentialCheckFailed
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, install, member

pytestmark = pytest.mark.asyncio

ODOO = {
    "url": "https://odoo.acme.test",
    "database": "acme",
    "username": "anna",
    "password": "s3cret",
}


@pytest.fixture
def odoo_validator(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Replace the Odoo type's live login check: wrong password -> an auth error."""
    seen: list[dict[str, str]] = []

    async def validate(values: dict[str, str]) -> None:
        seen.append(values)
        if values.get("password") != "s3cret":
            raise CredentialCheckFailed("Access Denied: wrong login/password")

    async def load(db: Any, *, tenant_id: Any, credential_type: str, cred_type: Any) -> Any:
        return validate

    monkeypatch.setattr("oc8.credentials.service._load_validator", load)
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: ["93.184.216.34"])
    return seen


def test_errors_are_sorted_into_what_a_person_can_fix() -> None:
    assert classify_check_error("Access Denied: wrong login/password") == "auth"
    assert classify_check_error("HTTP 401 Unauthorized") == "auth"
    assert (
        classify_check_error("ConnectError: [Errno 8] nodename nor servname provided")
        == "unreachable"
    )
    assert classify_check_error("timed out") == "unreachable"
    assert classify_check_error("database 'acme' does not exist") == "invalid"
    # whole words only: a digit run or a longer word must not trigger a class
    assert classify_check_error("record 14013 not found") == "invalid"
    assert classify_check_error("the sslab module is missing") == "invalid"
    assert classify_check_error("field url is required") == "invalid"


async def test_a_member_checks_then_saves_their_own_login(
    app_session: AppSessionFactory, odoo_validator: list[dict[str, str]]
) -> None:
    tenant = uuid.uuid4()
    anna = headers(tenant, "anna@acme.test")
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        bad = await c.post(
            "/api/v1/connections/credentials/check",
            json={"integration": "odoo_mcp", "fieldValues": {**ODOO, "password": "nope"}},
            headers=anna,
        )
        assert bad.json() == {
            "ok": False,
            "reason": "auth",
            "detail": "Access Denied: wrong login/password",
        }
        good = await c.post(
            "/api/v1/connections/credentials/check",
            json={"integration": "odoo_mcp", "fieldValues": ODOO},
            headers=anna,
        )
        assert good.json()["ok"] is True
        assert "s3cret" not in good.text and "s3cret" not in bad.text
        # The check stores nothing.
        assert (await c.get("/api/v1/connections/mine", headers=anna)).json() == []
        saved = await c.post(
            "/api/v1/connections/credentials",
            json={"integration": "odoo_mcp", "fieldValues": ODOO},
            headers=anna,
        )
        assert saved.status_code == 201, saved.text
        body = saved.json()
        assert (body["owner"], body["integration"], body["status"]) == ("me", "odoo_mcp", "ok")
        assert "s3cret" not in saved.text
        mine = (await c.get("/api/v1/connections/mine", headers=anna)).json()
        assert [row["id"] for row in mine] == [body["id"]]


async def test_a_member_cannot_create_an_organisation_access(
    app_session: AppSessionFactory, odoo_validator: list[dict[str, str]]
) -> None:
    tenant = uuid.uuid4()
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await c.post(
            "/api/v1/connections/credentials",
            json={"integration": "odoo_mcp", "owner": "organisation", "fieldValues": ODOO},
            headers=headers(tenant, "anna@acme.test"),
        )
    assert r.status_code == 403


async def test_an_admin_creates_the_organisation_access_with_a_free_name(
    app_session: AppSessionFactory, odoo_validator: list[dict[str, str]]
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    await credential_row(app_session, tenant, owner=None, name="Odoo")
    async with api() as c:
        await install(c, admin, "odoo_mcp")
        r = await c.post(
            "/api/v1/connections/credentials",
            json={
                "integration": "odoo_mcp",
                "owner": "organisation",
                "name": "Odoo",
                "fieldValues": ODOO,
            },
            headers=admin,
        )
    assert r.status_code == 201, r.text
    assert (r.json()["owner"], r.json()["accountLabel"]) == ("organisation", "Odoo 2")


async def test_testing_another_members_login_is_not_found(
    app_session: AppSessionFactory, odoo_validator: list[dict[str, str]]
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    await member(app_session, tenant, "ben@acme.test")
    annas = await credential_row(app_session, tenant, owner=anna)
    async with api() as c:
        r = await c.post(
            f"/api/v1/connections/credentials/{annas}/test",
            headers=headers(tenant, "ben@acme.test"),
        )
    assert r.status_code == 404
