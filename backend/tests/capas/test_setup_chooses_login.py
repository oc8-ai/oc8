"""A tool setup only chooses a login (unified connections design, "Storage").

The setup form enables the chosen organisation login for the department and
keeps the tool's one connection row free of login values; the new-agent
dialog's login becomes the agent's fixed choice instead of an extra row.

Built on a throwaway tool pack and login type, so no vendor is involved."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import config
from oc8 import models as m
from oc8.capas.discovery import invalidate_discovery_cache
from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.connections.access import binding_fingerprint
from oc8.credentials import registry
from oc8.credentials.enablement import department_ids_by_credential
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers, install

pytestmark = pytest.mark.asyncio

PLUGIN = "test_setup_login_pack"
KEY = "test_setup_login_tool"
TYPE = "test_setup_login"

PLUGIN_TOML = f"""
[plugin]
name = "{PLUGIN}"
label = "Test Setup Login"
version = "1.0.0"
type = "tool_pack"
trust = "first_party"
summary = "A throwaway tool pack whose setup chooses a login"
permissions = ["mcp:connect", "secrets:use"]
"""

TOOL_PACK_TOML = f"""
[[connections]]
key = "primary"
name = "{KEY}"
transport = "stdio"
server_url = ""
credential_type = "{TYPE}"

[connections.scopes]
read = ["look"]
modify = ["change"]

[connections.credential_env_fields]
LOGIN_URL = "url"
LOGIN_TOKEN = "token"

[connections.config]
command = "python"
args = ["-m", "bridge"]

[connections.config.env]
LOGIN_MODE = "plain"
"""

FIELDS_TOML = f"""
title = "Set up the test tool"

[[fields]]
key = "department"
label = "Department"
kind = "department"
required = false

[[fields]]
key = "system"
label = "System"
kind = "credential"
credential_type = "{TYPE}"
"""

MCP_TOML = f"""
connection_key = "primary"
name = "{KEY}"
command = "python"
args = ["-m", "bridge"]
department_field = "department"

[env]
LOGIN_MODE = "plain"

[env_fields]
LOGIN_URL = "url"

[secret_env_fields]
LOGIN_TOKEN = "system"
"""

FRAME = {"tools": {KEY: {"enabled": True, "read": True, "modify": True}}}


@pytest.fixture(autouse=True)
def _test_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    pack = tmp_path / "capas" / PLUGIN
    (pack / "setup").mkdir(parents=True)
    (pack / "plugin.toml").write_text(PLUGIN_TOML)
    (pack / "tool_pack.toml").write_text(TOOL_PACK_TOML)
    (pack / "setup" / "fields.toml").write_text(FIELDS_TOML)
    (pack / "setup" / "mcp.toml").write_text(MCP_TOML)
    real = Path(__file__).resolve().parents[3] / "capas"
    monkeypatch.setenv("OC8_CAPAS_PATH", f"{tmp_path / 'capas'},{real}")
    config.get_settings.cache_clear()
    settings = config.get_settings()
    kek = base64.b64encode(bytes(range(32))).decode()
    monkeypatch.setattr(settings, "secret_kek", kek, raising=False)
    invalidate_discovery_cache()
    registry.CORE_CREDENTIAL_TYPES[TYPE] = CredentialTypeSpec(
        name=TYPE,
        display_name="Test setup login",
        fields=[
            SetupFieldSpec(key="url", label="URL", kind="url"),
            SetupFieldSpec(key="token", label="Token", kind="password"),
        ],
    )
    try:
        yield
    finally:
        registry.CORE_CREDENTIAL_TYPES.pop(TYPE, None)
        config.get_settings.cache_clear()
        invalidate_discovery_cache()


async def _login(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, name: str, url: str = "https://svc.test"
) -> uuid.UUID:
    """An organisation login, stored as the credentials API would (the
    token's secret under the login's own ref)."""
    async with app_session(tenant) as db:
        row = m.Credential(
            tenant_id=tenant,
            name=name,
            credential_type=TYPE,
            field_values={"url": url} if url else {},
            secret_refs={},
        )
        db.add(row)
        await db.flush()
        ref = f"credential/{row.id}/token"
        await store_secret(db, tenant_id=tenant, name=ref, value="tok-secret", kind="credential")
        row.secret_refs = {"token": ref}
        return row.id


async def _departments(
    app_session: AppSessionFactory, tenant: uuid.UUID, *names: str
) -> list[uuid.UUID]:
    async with app_session(tenant) as db:
        rows = [m.Department(tenant_id=tenant, name=n, frame=FRAME) for n in names]
        db.add_all(rows)
        await db.flush()
        return [r.id for r in rows]


async def _tool_rows(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.McpConnection]:
    async with app_session(tenant) as db:
        return list(
            (await db.execute(select(m.McpConnection).where(m.McpConnection.name == KEY)))
            .scalars()
            .all()
        )


def _env(row: m.McpConnection) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = row.config or {}
    return dict(cfg.get("env") or {}), dict(cfg.get("secret_env") or {})


async def test_setup_for_a_second_department_adds_an_enablement(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    sales, marketing = await _departments(app_session, tenant, "Sales", "Marketing")
    login = await _login(app_session, tenant, name="Company system")
    async with api() as c:
        capa_id = await install(c, admin, PLUGIN)
        for department in (sales, marketing):
            r = await c.post(
                f"/api/v1/capas/{capa_id}/setup",
                json={"values": {"department": str(department), "system": str(login)}},
                headers=admin,
            )
            assert r.status_code == 200, r.text
            assert r.json()["credentialId"] == str(login)
    async with app_session(tenant) as db:
        enabled = (await department_ids_by_credential(db, [login]))[login]
        cred = await db.get(m.Credential, login)
        assert cred is not None and cred.all_departments is False
    # The second department is added; the first keeps the login.
    assert set(enabled) == {sales, marketing}
    rows = await _tool_rows(app_session, tenant)
    # One row for the tool, in no department: never one per department.
    assert [(r.department_id, r.credential_id) for r in rows] == [(None, None)]

    # Without a department, the login is enabled for all of them.
    async with api() as c:
        r = await c.post(
            f"/api/v1/capas/{capa_id}/setup",
            json={"values": {"department": "", "system": str(login)}},
            headers=admin,
        )
        assert r.status_code == 200, r.text
    async with app_session(tenant) as db:
        cred = await db.get(m.Credential, login)
        assert cred is not None and cred.all_departments is True
        assert set((await department_ids_by_credential(db, [login]))[login]) == {sales, marketing}


async def test_setup_with_an_incomplete_login_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    (sales,) = await _departments(app_session, tenant, "Sales")
    login = await _login(app_session, tenant, name="Half a login", url="")
    async with api() as c:
        capa_id = await install(c, admin, PLUGIN)
        before = [
            (r.id, r.department_id, r.config, r.connected)
            for r in await _tool_rows(app_session, tenant)
        ]
        r = await c.post(
            f"/api/v1/capas/{capa_id}/setup",
            json={"values": {"department": str(sales), "system": str(login)}},
            headers=admin,
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"code": "credential_incomplete", "missing": ["url"]}
    after = [
        (r.id, r.department_id, r.config, r.connected)
        for r in await _tool_rows(app_session, tenant)
    ]
    assert after == before
    async with app_session(tenant) as db:
        assert (await department_ids_by_credential(db, [login]))[login] == []
        cred = await db.get(m.Credential, login)
        assert cred is not None and cred.all_departments is False


async def test_setup_stores_no_copied_credential_values(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    (sales,) = await _departments(app_session, tenant, "Sales")
    login = await _login(app_session, tenant, name="Company system")
    async with api() as c:
        capa_id = await install(c, admin, PLUGIN)
        r = await c.post(
            f"/api/v1/capas/{capa_id}/setup",
            json={"values": {"department": str(sales), "system": str(login)}},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        assert r.json()["credentialId"] == str(login)
        secrets = await c.get("/api/v1/secrets", headers=admin)
        assert secrets.status_code == 200
        assert not [s["name"] for s in secrets.json() if s["name"].startswith("plugin:")]
    (row,) = await _tool_rows(app_session, tenant)
    assert str(row.id) == r.json()["connectionId"]
    env, secret_env = _env(row)
    # The pack's own launch env survives; no value of the login is copied.
    assert env.get("LOGIN_MODE") == "plain"
    assert not {"LOGIN_URL", "LOGIN_TOKEN"} & (set(env) | set(secret_env))
    assert "https://svc.test" not in env.values()


async def test_new_agent_login_becomes_a_fixed_choice(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    (sales,) = await _departments(app_session, tenant, "Sales")
    login = await _login(app_session, tenant, name="Company system")
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=sales, name="Mia")
        db.add(agent)
        await db.flush()
        agent_id = agent.id
    async with api() as c:
        await install(c, admin, PLUGIN)
        rows_before = {r.id for r in await _tool_rows(app_session, tenant)}
        r = await c.post(
            "/api/v1/mcp/logins",
            json={
                "name": KEY,
                "credentialType": TYPE,
                "credentialId": str(login),
                "agentId": str(agent_id),
                "departmentId": str(sales),
            },
            headers=admin,
        )
        assert r.status_code == 201, r.text
        assert r.json()["credentialId"] == str(login)

        # For a department (its default login), the login is enabled for it.
        r3 = await c.post(
            "/api/v1/mcp/logins",
            json={
                "name": KEY,
                "credentialType": TYPE,
                "credentialId": str(login),
                "departmentId": str(sales),
            },
            headers=admin,
        )
        assert r3.status_code == 201, r3.text

        # Without an agent, only the login is created and returned.
        r2 = await c.post(
            "/api/v1/mcp/logins",
            json={
                "name": KEY,
                "credentialType": TYPE,
                "fieldValues": {"url": "https://x.test", "token": "t"},
            },
            headers=admin,
        )
        assert r2.status_code == 201, r2.text
    rows = await _tool_rows(app_session, tenant)
    assert {r.id for r in rows} == rows_before  # no McpConnection per login
    assert all(r.credential_id is None for r in rows)
    async with app_session(tenant) as db:
        access = (
            await db.execute(
                select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent_id)
            )
        ).scalar_one()
        assert (access.tool_key, access.mode, access.credential_id) == (KEY, "fixed", login)
        assert access.oauth_connection_id is None
        assert access.set_with_manage is True
        live = await db.get(m.Agent, agent_id)
        assert live is not None
        assert access.binding_fingerprint == await binding_fingerprint(
            db, live, KEY, version_id=live.current_version_id
        )
        assert (await department_ids_by_credential(db, [login]))[login] == [sales]
        created = await db.get(m.Credential, uuid.UUID(r2.json()["credentialId"]))
        assert created is not None and created.credential_type == TYPE


async def test_testing_the_tool_with_a_chosen_login_leaves_its_row_to_the_others(
    app_session: AppSessionFactory,
) -> None:
    """Save & test tests the tool's row with the login the setup chose; a
    failing login does not disconnect the row every department shares."""
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    login = await _login(app_session, tenant, name="Company system")
    half = await _login(app_session, tenant, name="Half a login", url="")
    async with api() as c:
        await install(c, admin, PLUGIN)
        (row,) = await _tool_rows(app_session, tenant)
        async with app_session(tenant) as db:
            live = await db.get(m.McpConnection, row.id)
            assert live is not None
            live.connected = True
        refused = await c.post(
            f"/api/v1/mcp/connections/{row.id}/test",
            params={"credentialId": str(half)},
            headers=admin,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == {"code": "credential_incomplete", "missing": ["url"]}
        tested = await c.post(
            f"/api/v1/mcp/connections/{row.id}/test",
            params={"credentialId": str(login)},
            headers=admin,
        )
        assert tested.status_code == 200, tested.text
        # The throwaway bridge does not exist: the answer carries the error.
        assert tested.json()["connected"] is False
        assert tested.json()["health"]["status"] == "error"
    (after,) = await _tool_rows(app_session, tenant)
    assert after.connected is True


async def _refuse_the_token(values: dict[str, str]) -> None:
    from oc8.credentials.errors import CredentialCheckFailed

    raise CredentialCheckFailed("The token was refused.", reason="auth")


async def test_save_and_test_runs_the_login_check_connections_runs(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E F8: Save & test only started the tool's bridge, which comes up
    whatever the login holds, and answered "connected" for a wrong password
    that Connections -> Test then refused. A login type that declares a check
    is now checked first, the same check, and its answer is the test's."""
    monkeypatch.setitem(
        registry.CORE_CREDENTIAL_TYPES,
        TYPE,
        registry.CORE_CREDENTIAL_TYPES[TYPE].model_copy(
            update={
                "validate_entry_point": ("tests.capas.test_setup_chooses_login:_refuse_the_token")
            }
        ),
    )
    # The login's address is a made-up host; the outbound guard is not what
    # this test is about.
    monkeypatch.setattr("oc8.credentials.service._guard_url_fields", lambda *_: None)
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    login = await _login(app_session, tenant, name="Company system")
    async with api() as c:
        await install(c, admin, PLUGIN)
        (row,) = await _tool_rows(app_session, tenant)
        tested = await c.post(
            f"/api/v1/mcp/connections/{row.id}/test",
            params={"credentialId": str(login)},
            headers=admin,
        )
    assert tested.status_code == 200, tested.text
    assert tested.json()["connected"] is False
    assert tested.json()["health"]["status"] == "error"
    assert "The token was refused." in tested.json()["health"]["error"]
    async with app_session(tenant) as db:
        cred = await db.get(m.Credential, login)
        assert cred is not None
        # Recorded as Connections -> Test records it.
        assert cred.last_test_ok is False


async def test_binding_a_new_login_to_an_agent_needs_the_right_to_edit_that_agent(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POST /mcp/logins` with an agent is the same door as the agent's own
    tool-access write: integration rights alone do not let a caller change an
    agent they may not edit."""
    import dataclasses

    from oc8.api import deps
    from oc8.authz.permissions import AGENT, MANAGE, perm

    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    (sales,) = await _departments(app_session, tenant, "Sales")
    login = await _login(app_session, tenant, name="Company system")
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=sales, name="Mia")
        db.add(agent)
        await db.flush()
        agent_id = agent.id
    real = deps._authority

    async def without_agent_manage(request: Any, db: Any, principal: Any) -> Any:
        authority = await real(request, db, principal)
        return dataclasses.replace(
            authority, tenant_wide=authority.tenant_wide - {perm(AGENT, MANAGE)}
        )

    async with api() as c:
        await install(c, admin, PLUGIN)
        monkeypatch.setattr(deps, "_authority", without_agent_manage)
        r = await c.post(
            "/api/v1/mcp/logins",
            json={
                "name": KEY,
                "credentialType": TYPE,
                "fieldValues": {"url": "https://x.test", "token": "t"},
                "agentId": str(agent_id),
            },
            headers=admin,
        )
    assert r.status_code in (403, 404), r.text
    async with app_session(tenant) as db:
        rows = (
            await db.execute(
                select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent_id)
            )
        ).all()
        assert rows == []
        creds = (await db.execute(select(m.Credential.id))).scalars().all()
        assert creds == [login]  # no login was created either


async def test_testing_a_shared_tool_row_without_a_login_does_not_disconnect_it(
    app_session: AppSessionFactory,
) -> None:
    """The Connections page "Test" names no login: the shared row of a login
    tool is tested with the login it would use, asks which one when several
    could be, and a failure never takes the row away from every department."""
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        await install(c, admin, PLUGIN)
        (row,) = await _tool_rows(app_session, tenant)
        async with app_session(tenant) as db:
            live = await db.get(m.McpConnection, row.id)
            assert live is not None
            live.connected = True

        # No login at all: nothing to test with.
        none = await c.post(f"/api/v1/mcp/connections/{row.id}/test", headers=admin)
        assert none.status_code == 422, none.text
        assert none.json()["detail"]["code"] == "login_required"

        # The only login: tested with it; the failure stays in the answer.
        await _login(app_session, tenant, name="Company system")
        tested = await c.post(f"/api/v1/mcp/connections/{row.id}/test", headers=admin)
        assert tested.status_code == 200, tested.text
        assert tested.json()["connected"] is False
        assert tested.json()["health"]["status"] == "error"

        # Several logins, none enabled: the caller has to choose.
        await _login(app_session, tenant, name="Second system")
        several = await c.post(f"/api/v1/mcp/connections/{row.id}/test", headers=admin)
        assert several.status_code == 422, several.text
        assert several.json()["detail"]["code"] == "login_required"
    (after,) = await _tool_rows(app_session, tenant)
    assert after.connected is True


async def test_setup_never_writes_to_a_retired_row(app_session: AppSessionFactory) -> None:
    """A row consolidation retired (still named by an unfinished run) keeps the
    plugin's stamp but is no tool row: a setup must never land on it."""
    import datetime as dt

    from oc8.connections.tool_connection import RETIRED_KEY

    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    (sales,) = await _departments(app_session, tenant, "Sales")
    login = await _login(app_session, tenant, name="Company system")
    async with api() as c:
        capa_id = await install(c, admin, PLUGIN)
        (tool,) = await _tool_rows(app_session, tenant)
        async with app_session(tenant) as db:
            retired = m.McpConnection(
                tenant_id=tenant,
                name=KEY,
                server_url=tool.server_url,
                transport=tool.transport,
                scopes=tool.scopes,
                config={**(tool.config or {}), RETIRED_KEY: str(tool.id)},
                connected=False,
                # Older than the tool row: an unfiltered "oldest" pick takes it.
                created_at=tool.created_at - dt.timedelta(days=1),
            )
            db.add(retired)
            await db.flush()
            retired_id, retired_config = retired.id, dict(retired.config)
        r = await c.post(
            f"/api/v1/capas/{capa_id}/setup",
            json={"values": {"department": str(sales), "system": str(login)}},
            headers=admin,
        )
        assert r.status_code == 200, r.text
    async with app_session(tenant) as db:
        untouched = await db.get(m.McpConnection, retired_id)
        assert untouched is not None
        assert untouched.config == retired_config
        assert untouched.department_id is None and not untouched.connected
    live = [r for r in await _tool_rows(app_session, tenant) if RETIRED_KEY not in (r.config or {})]
    assert [r.id for r in live] == [tool.id]


async def test_choosing_another_login_for_a_department_replaces_the_old_one(
    app_session: AppSessionFactory,
) -> None:
    """The setup names ONE login for the department: choosing another one
    replaces the first for that department (never both, which would make it
    ambiguous). Another department keeps the first; a login enabled for all
    departments is left alone."""
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    sales, marketing = await _departments(app_session, tenant, "Sales", "Marketing")
    first = await _login(app_session, tenant, name="First")
    second = await _login(app_session, tenant, name="Second", url="https://other.test")
    everyone = await _login(app_session, tenant, name="Everyone", url="https://all.test")
    async with app_session(tenant) as db:
        cred = await db.get(m.Credential, everyone)
        assert cred is not None
        cred.all_departments = True
    async with api() as c:
        capa_id = await install(c, admin, PLUGIN)
        for department, login in ((sales, first), (marketing, first), (sales, second)):
            r = await c.post(
                f"/api/v1/capas/{capa_id}/setup",
                json={"values": {"department": str(department), "system": str(login)}},
                headers=admin,
            )
            assert r.status_code == 200, r.text
    async with app_session(tenant) as db:
        enabled = await department_ids_by_credential(db, [first, second, everyone])
        cred = await db.get(m.Credential, everyone)
        assert cred is not None and cred.all_departments is True
    assert enabled[first] == [marketing]
    assert enabled[second] == [sales]
    assert enabled[everyone] == []
