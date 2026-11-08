"""The check/test/create routes run a validator on values any member typed in:
they must not reach internal addresses and must not echo upstream text."""

from __future__ import annotations

import ipaddress
import ssl
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.credentials.errors import CredentialCheckFailed
from oc8.net.outbound import guard_outbound_url
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, install, member

pytestmark = pytest.mark.asyncio

ODOO = {
    "url": "https://odoo.acme.test",
    "database": "acme",
    "username": "anna",
    "password": "s3cret",
}
PUBLIC = "93.184.216.34"

Validator = Callable[[dict[str, str]], Awaitable[None]]


class Spy:
    """Stands in for the credential type's validator and records its calls."""

    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []
        self.fn: Validator = self._ok

    async def _ok(self, values: dict[str, str]) -> None:
        self.seen.append(values)

    async def load(
        self, db: Any, *, tenant_id: Any, credential_type: str, cred_type: Any
    ) -> Validator:
        async def run(values: dict[str, str]) -> None:
            if self.fn == self._ok:
                self.seen.append(values)
                return
            await self.fn(values)

        return run


def _resolver(host: str) -> list[str]:
    if host == "localhost":
        return ["127.0.0.1"]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return [PUBLIC]
    return [host]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    """Hostnames resolve to a public address (IP literals and localhost stay
    what they are); the validator is a spy."""
    s = Spy()
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", _resolver)
    monkeypatch.setattr("oc8.credentials.service._load_validator", s.load)
    return s


async def _check(c: httpx.AsyncClient, h: dict[str, str], values: dict[str, Any]) -> httpx.Response:
    return await c.post(
        "/api/v1/connections/credentials/check",
        json={"integration": "odoo_mcp", "fieldValues": values},
        headers=h,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8069",
        "http://127.0.0.1",
        "http://10.0.0.5:8069",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]:8069",
        "http://[fd00::1]",
        "file:///etc/passwd",
    ],
)
async def test_internal_addresses_are_never_checked(
    app_session: AppSessionFactory, spy: Spy, url: str
) -> None:
    tenant = uuid.uuid4()
    anna = headers(tenant, "anna@acme.test")
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await _check(c, anna, {**ODOO, "url": url})
    assert r.json() == {"ok": False, "reason": "unreachable", "detail": None}
    assert spy.seen == []


async def test_a_hostname_that_resolves_to_a_private_address_is_refused(
    app_session: AppSessionFactory, spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: [PUBLIC, "10.1.2.3"])
    tenant = uuid.uuid4()
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await _check(c, headers(tenant, "anna@acme.test"), ODOO)
    assert r.json()["reason"] == "unreachable"
    assert spy.seen == []


async def test_saving_a_blocked_url_never_runs_the_validator_and_is_marked_failed(
    app_session: AppSessionFactory, spy: Spy
) -> None:
    tenant = uuid.uuid4()
    anna = headers(tenant, "anna@acme.test")
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await c.post(
            "/api/v1/connections/credentials",
            json={"integration": "odoo_mcp", "fieldValues": {**ODOO, "url": "http://10.0.0.5"}},
            headers=anna,
        )
        assert r.status_code == 201
        assert r.json()["status"] == "failed"
        t = await c.post(f"/api/v1/connections/credentials/{r.json()['id']}/test", headers=anna)
    assert t.json() == {"ok": False, "reason": "unreachable", "detail": None}
    assert spy.seen == []


async def test_a_public_host_passes_the_guard_and_the_validator_runs(
    app_session: AppSessionFactory, spy: Spy
) -> None:
    tenant = uuid.uuid4()
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await _check(c, headers(tenant, "anna@acme.test"), ODOO)
    assert r.json()["ok"] is True
    assert spy.seen == [ODOO]


async def test_upstream_text_never_reaches_the_response(
    app_session: AppSessionFactory, spy: Spy
) -> None:
    async def boom(values: dict[str, str]) -> None:
        raise httpx.HTTPStatusError(
            "500 for http://10.0.0.9:8069/jsonrpc: token=sk-live-abc123",
            request=httpx.Request("POST", "http://10.0.0.9:8069/jsonrpc"),
            response=httpx.Response(500, text="sk-live-abc123"),
        )

    spy.fn = boom
    tenant = uuid.uuid4()
    anna = headers(tenant, "anna@acme.test")
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await _check(c, anna, ODOO)
        saved = await c.post(
            "/api/v1/connections/credentials",
            json={"integration": "odoo_mcp", "fieldValues": ODOO},
            headers=anna,
        )
        t = await c.post(f"/api/v1/connections/credentials/{saved.json()['id']}/test", headers=anna)
    for body in (r.text, saved.text, t.text):
        assert "sk-live-abc123" not in body and "10.0.0.9" not in body
    assert r.json()["ok"] is False and r.json()["detail"] is None
    assert t.json()["detail"] is None


async def test_json_decode_errors_are_not_treated_as_user_facing(
    app_session: AppSessionFactory, spy: Spy
) -> None:
    import json

    async def bad(values: dict[str, str]) -> None:
        json.loads("<html>internal 10.0.0.9</html>")

    spy.fn = bad
    tenant = uuid.uuid4()
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await _check(c, headers(tenant, "anna@acme.test"), ODOO)
    assert r.json()["detail"] is None


async def test_a_validators_own_value_error_is_shown(
    app_session: AppSessionFactory, spy: Spy
) -> None:
    async def nope(values: dict[str, str]) -> None:
        raise CredentialCheckFailed(
            "Odoo rejected this login: invalid username, password, or API key"
        )

    spy.fn = nope
    tenant = uuid.uuid4()
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await _check(c, headers(tenant, "anna@acme.test"), ODOO)
    assert r.json() == {
        "ok": False,
        "reason": "auth",
        "detail": "Odoo rejected this login: invalid username, password, or API key",
    }


async def test_a_missing_required_field_is_invalid_on_check_and_422_on_save(
    app_session: AppSessionFactory, spy: Spy
) -> None:
    tenant = uuid.uuid4()
    anna = headers(tenant, "anna@acme.test")
    partial = {k: v for k, v in ODOO.items() if k != "password"}
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        chk = await _check(c, anna, partial)
        assert chk.json()["ok"] is False and chk.json()["reason"] == "invalid"
        r = await c.post(
            "/api/v1/connections/credentials",
            json={"integration": "odoo_mcp", "fieldValues": partial},
            headers=anna,
        )
        assert r.status_code == 422
        assert (await c.get("/api/v1/connections/mine", headers=anna)).json() == []


async def test_without_a_name_the_integration_label_is_used_and_creation_is_audited(
    app_session: AppSessionFactory, spy: Spy
) -> None:
    tenant = uuid.uuid4()
    anna = headers(tenant, "anna@acme.test")
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        label = next(
            i["label"]
            for i in (await c.get("/api/v1/connections/integrations", headers=anna)).json()
            if i["name"] == "odoo_mcp"
        )
        r = await c.post(
            "/api/v1/connections/credentials",
            json={"integration": "odoo_mcp", "fieldValues": ODOO},
            headers=anna,
        )
    assert r.json()["accountLabel"] == label
    async with app_session(tenant) as db:
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "connection.created")
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].resource["id"] == r.json()["id"]
    assert "s3cret" not in str(events[0].resource)


async def test_a_name_taken_in_a_race_gets_the_next_suffix_not_a_500(
    app_session: AppSessionFactory, spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    anna_id = await member(app_session, tenant, "anna@acme.test")
    await credential_row(app_session, tenant, owner=anna_id, name="Odoo")
    import oc8.api.v1.connections as conns

    real = conns._free_name
    first = {"done": False}

    async def racy(db: Any, tenant_id: Any, owner: Any, base: str) -> str:
        if not first["done"]:
            first["done"] = True
            return base  # as if the other request had not committed yet
        return await real(db, tenant_id, owner, base)

    monkeypatch.setattr(conns, "_free_name", racy)
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await c.post(
            "/api/v1/connections/credentials",
            json={"integration": "odoo_mcp", "name": "Odoo", "fieldValues": ODOO},
            headers=headers(tenant, "anna@acme.test"),
        )
    assert r.status_code == 201, r.text
    assert r.json()["accountLabel"] == "Odoo 2"


def test_guard_returns_the_checked_address() -> None:
    assert guard_outbound_url("https://x.test/a", resolver=lambda h: [PUBLIC]) == PUBLIC


async def test_a_plain_value_error_is_not_user_facing(
    app_session: AppSessionFactory, spy: Spy
) -> None:
    async def leaky(values: dict[str, str]) -> None:
        raise ValueError("could not reach http://10.0.0.9:8069: token sk-live-1")

    spy.fn = leaky
    tenant = uuid.uuid4()
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await _check(c, headers(tenant, "anna@acme.test"), ODOO)
    assert r.json()["detail"] is None
    assert "sk-live-1" not in r.text and "10.0.0.9" not in r.text


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ConnectTimeout("t"), "unreachable"),
        (httpx.ReadTimeout("t"), "unreachable"),
        (ssl.SSLError("certificate verify failed"), "unreachable"),
        (ConnectionRefusedError("x"), "unreachable"),
        (
            httpx.HTTPStatusError(
                "x", request=httpx.Request("GET", "http://a"), response=httpx.Response(401)
            ),
            "auth",
        ),
        (RuntimeError("something odd"), "invalid"),
    ],
)
async def test_the_reason_comes_from_the_exception_type(
    app_session: AppSessionFactory, spy: Spy, exc: Exception, reason: str
) -> None:
    async def fail(values: dict[str, str]) -> None:
        raise exc

    spy.fn = fail
    tenant = uuid.uuid4()
    async with api() as c:
        await install(c, headers(tenant, "admin@acme.test", "org_admin"), "odoo_mcp")
        r = await _check(c, headers(tenant, "anna@acme.test"), ODOO)
    assert r.json() == {"ok": False, "reason": reason, "detail": None}


async def test_a_host_field_is_guarded_like_a_url(app_session: AppSessionFactory, spy: Spy) -> None:
    from oc8.credentials.service import check_credential_values

    tenant = uuid.uuid4()
    values = {"host": "10.0.0.5", "port": "587", "use_tls": "true", "from_address": "a@acme.test"}
    async with app_session(tenant) as db:
        with pytest.raises(CredentialCheckFailed) as err:
            await check_credential_values(
                db, tenant_id=tenant, credential_type="smtp_server", field_values=values
            )
        assert (err.value.reason, err.value.detail) == ("unreachable", None)
        await check_credential_values(
            db,
            tenant_id=tenant,
            credential_type="smtp_server",
            field_values={**values, "host": "mail.acme.test"},
        )
    assert [v["host"] for v in spy.seen] == ["mail.acme.test"]


def test_a_scheme_less_url_value_is_guarded_but_not_rewritten(spy: Spy) -> None:
    from types import SimpleNamespace

    from oc8.credentials.service import _guard_url_fields

    cred_type = SimpleNamespace(fields=[SimpleNamespace(key="endpoint", kind="url")])
    values = {"endpoint": "minio:9000"}
    _guard_url_fields(cred_type, values)
    assert values == {"endpoint": "minio:9000"}
    with pytest.raises(CredentialCheckFailed):
        _guard_url_fields(cred_type, {"endpoint": "localhost:9000"})
