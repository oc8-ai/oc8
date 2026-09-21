# backend/tests/api/test_mcp_external.py
from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.requests import Request

from oc8 import models as m
from oc8.api.mcp_external import _verify_api_key
from oc8.apikeys.service import create_api_key
from oc8.authz.permissions import MEMBER_ROLE
from oc8.config import get_settings
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# tests/api/<this file> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _plugins_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`copilot_list_connection_tools` resolves the real odoo_mcp manifest --
    same setup as `tests/copilot/test_proposals.py`'s `agent.guardrail.set`
    tests, which read the same manifest through the same code path."""
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_PLUGINS_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client(app: object) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _sole_organization(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    """Leave `tenant` as the instance's ONLY Organization.

    `_verify_api_key` resolves the singleton Organization the same way
    `password_login` does, and every test above (plus most of the suite)
    leaves its own random-tenant Organization behind. The wipe runs on an
    owner-role, RLS-exempt connection because an app-role session bound to
    one tenant cannot see -- let alone delete -- another tenant's row. Same
    pattern as `tests/auth/test_password_auth.py::org_in_db` and
    `tests/api/test_auth_self_service.py::_sole_organization`.
    """
    owner_engine = create_async_engine(get_settings().migration_async_url, poolclass=NullPool)
    try:
        async with AsyncSession(owner_engine, expire_on_commit=False) as owner:
            await owner.execute(text("DELETE FROM org_member"))
            await owner.execute(text("DELETE FROM role"))
            await owner.execute(text("DELETE FROM organization"))
            await owner.commit()
    finally:
        await owner_engine.dispose()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=str(tenant), name="t"))


async def _member_with_role(
    app_session: AppSessionFactory, tenant: uuid.UUID, subject: str, role_name: str
) -> uuid.UUID:
    async with app_session(tenant) as db:
        role = m.Role(tenant_id=tenant, name=role_name, builtin=True)
        db.add(role)
        await db.flush()
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:local:{subject}"),
            role_id=role.id,
        )
        db.add(member)
        await db.flush()
        return member.id


async def _api_key_token(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    member_id: uuid.UUID,
    *,
    allowed_origins: list[str] | None = None,
    enabled: bool = True,
    expires_at: dt.datetime | None = None,
) -> str:
    async with app_session(tenant) as db:
        row, token = await create_api_key(
            db,
            tenant_id=tenant,
            member_id=member_id,
            name="k",
            allowed_origins=allowed_origins,
            expires_at=expires_at,
        )
        row.enabled = enabled
        await db.flush()
    return token


async def _admin_setup(
    app_session: AppSessionFactory, subject: str = "admin@example.com"
) -> tuple[uuid.UUID, str]:
    """A fresh, sole-Organization tenant with one org_admin-rooted API key."""
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, subject, "org_admin")
    token = await _api_key_token(app_session, tenant, member_id)
    return tenant, token


async def test_initialize_returns_protocol_version_and_server_name(
    app_session: AppSessionFactory,
) -> None:
    _tenant, token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["serverInfo"]["name"] == "oc8-external-copilot-gateway"
    assert body["result"]["protocolVersion"] == "2025-06-18"


async def test_ping_returns_an_empty_result(app_session: AppSessionFactory) -> None:
    _tenant, token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text
    assert r.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


async def test_tools_list_returns_all_thirteen_copilot_tools(
    app_session: AppSessionFactory,
) -> None:
    _tenant, token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": f"Bearer {token}"},
            )
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {
        "copilot_propose",
        "copilot_list_proposals",
        "copilot_review_proposal",
        "copilot_apply_proposal",
        "copilot_reject_proposal",
        "copilot_list_departments",
        "copilot_get_department",
        "copilot_list_agents",
        "copilot_get_agent",
        "copilot_list_plugins",
        "copilot_get_plugin",
        "copilot_list_integrations",
        "copilot_list_connection_tools",
    }


async def test_copilot_list_departments_returns_tenant_departments(
    app_session: AppSessionFactory,
) -> None:
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name="Ops", goal="Keep the lights on")
        db.add(department)
        await db.flush()
        department_id = department.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "copilot_list_departments", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text
    departments = json.loads(r.json()["result"]["content"][0]["text"])
    assert [d["id"] for d in departments] == [str(department_id)]
    assert departments[0]["name"] == "Ops"
    assert departments[0]["goal"] == "Keep the lights on"


async def test_copilot_get_department_reads_by_id_and_never_another_tenants_row(
    app_session: AppSessionFactory,
) -> None:
    tenant, token = await _admin_setup(app_session)
    other_tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name="Ops")
        db.add(department)
        await db.flush()
        department_id = department.id
    async with app_session(other_tenant) as db:
        foreign = m.Department(tenant_id=other_tenant, name="Foreign")
        db.add(foreign)
        await db.flush()
        foreign_id = foreign.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            found = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_get_department",
                        "arguments": {"departmentId": str(department_id)},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            leaked = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_get_department",
                        "arguments": {"departmentId": str(foreign_id)},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    found_result = found.json()["result"]
    assert json.loads(found_result["content"][0]["text"])["id"] == str(department_id)
    leaked_result = leaked.json()["result"]
    assert leaked_result["isError"] is True
    assert "not found" in leaked_result["content"][0]["text"]


async def test_copilot_list_agents_filters_by_department(app_session: AppSessionFactory) -> None:
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        dept_a = m.Department(tenant_id=tenant, name="A")
        dept_b = m.Department(tenant_id=tenant, name="B")
        db.add_all([dept_a, dept_b])
        await db.flush()
        agent_a = m.Agent(tenant_id=tenant, department_id=dept_a.id, name="Ann")
        agent_b = m.Agent(tenant_id=tenant, department_id=dept_b.id, name="Bo")
        db.add_all([agent_a, agent_b])
        await db.flush()
        dept_a_id, agent_a_id, agent_b_id = dept_a.id, agent_a.id, agent_b.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            unfiltered = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "copilot_list_agents", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            filtered = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_list_agents",
                        "arguments": {"departmentId": str(dept_a_id)},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    unfiltered_ids = {
        a["id"] for a in json.loads(unfiltered.json()["result"]["content"][0]["text"])
    }
    assert unfiltered_ids == {str(agent_a_id), str(agent_b_id)}
    filtered_ids = {a["id"] for a in json.loads(filtered.json()["result"]["content"][0]["text"])}
    assert filtered_ids == {str(agent_a_id)}


async def test_copilot_get_agent_returns_the_agent_and_errors_for_an_unknown_id(
    app_session: AppSessionFactory,
) -> None:
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Target")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            found = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_get_agent",
                        "arguments": {"agentId": str(agent_id)},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            missing = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_get_agent",
                        "arguments": {"agentId": str(uuid.uuid4())},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    found_result = found.json()["result"]
    assert json.loads(found_result["content"][0]["text"])["id"] == str(agent_id)
    missing_result = missing.json()["result"]
    assert missing_result["isError"] is True
    assert "not found" in missing_result["content"][0]["text"]


async def test_copilot_list_plugins_and_get_plugin(app_session: AppSessionFactory) -> None:
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        capa = m.Capa(tenant_id=tenant, name="Odoo MCP", type="connector", trust_level="verified")
        db.add(capa)
        await db.flush()
        capa_id = capa.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            listed = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "copilot_list_plugins", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            got = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_get_plugin",
                        "arguments": {"pluginId": str(capa_id)},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            missing = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_get_plugin",
                        "arguments": {"pluginId": str(uuid.uuid4())},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    listed_plugins = json.loads(listed.json()["result"]["content"][0]["text"])
    assert [p["id"] for p in listed_plugins] == [str(capa_id)]
    assert listed_plugins[0]["trustLevel"] == "verified"
    got_plugin = json.loads(got.json()["result"]["content"][0]["text"])
    assert got_plugin["id"] == str(capa_id)
    missing_result = missing.json()["result"]
    assert missing_result["isError"] is True
    assert "not found" in missing_result["content"][0]["text"]


async def test_copilot_list_integrations_resolves_used_by_to_agent_ids(
    app_session: AppSessionFactory,
) -> None:
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Sina",
            presentation={"slug": "sina"},
        )
        db.add(agent)
        await db.flush()
        integration = m.Integration(
            tenant_id=tenant, key="odoo", name="Odoo", category="erp", used_by=["sina"]
        )
        db.add(integration)
        await db.flush()
        agent_id, integration_id = agent.id, integration.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "copilot_list_integrations", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    integrations = json.loads(r.json()["result"]["content"][0]["text"])
    assert [i["id"] for i in integrations] == [str(integration_id)]
    # `by_alias=True` -- same camelCase `IntegrationDTO` shape the
    # frontend-facing REST route (`GET /integrations`) serializes.
    assert integrations[0]["usedBy"] == [str(agent_id)]


async def test_copilot_list_connection_tools_returns_the_manifest_functions(
    app_session: AppSessionFactory,
) -> None:
    assert _PLUGINS_DIR.is_dir(), _PLUGINS_DIR
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="odoo",
            server_url="",
            transport="stdio",
            config={"_plugin_name": "odoo_mcp", "_connection_key": "primary"},
        )
        db.add(conn)
        await db.flush()

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            found = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_list_connection_tools",
                        "arguments": {"connectionName": "odoo"},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            missing = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_list_connection_tools",
                        "arguments": {"connectionName": "does-not-exist"},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    found_payload = json.loads(found.json()["result"]["content"][0]["text"])
    assert found_payload["connectionName"] == "odoo"
    assert "post_message" in found_payload["tools"]
    missing_result = missing.json()["result"]
    assert missing_result["isError"] is True
    assert "not found" in missing_result["content"][0]["text"]


async def test_a_notification_without_an_id_gets_a_bare_202(
    app_session: AppSessionFactory,
) -> None:
    _tenant, token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "method": "initialize"},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 202
    assert r.text == ""


async def test_an_unknown_method_gets_a_json_rpc_error_not_an_http_error(
    app_session: AppSessionFactory,
) -> None:
    _tenant, token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 7, "method": "nonsense"},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text
    assert r.json()["error"]["code"] == -32601


async def test_missing_bearer_token_is_401(app_session: AppSessionFactory) -> None:
    _tenant, _token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
            )
    assert r.status_code == 401


async def test_a_token_without_the_api_key_prefix_is_401(app_session: AppSessionFactory) -> None:
    _tenant, _token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer not-an-oc8-key"},
            )
    assert r.status_code == 401


async def test_an_unknown_token_is_401(app_session: AppSessionFactory) -> None:
    _tenant, _token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer oc8_ak_totally-made-up"},
            )
    assert r.status_code == 401


async def test_a_disabled_key_is_401(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "disabled@example.com", "org_admin")
    token = await _api_key_token(app_session, tenant, member_id, enabled=False)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 401


async def test_an_expired_key_is_401(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "expired@example.com", "org_admin")
    token = await _api_key_token(
        app_session,
        tenant,
        member_id,
        expires_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1),
    )
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 401


async def test_a_key_with_a_future_expiry_still_works(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "not-yet@example.com", "org_admin")
    token = await _api_key_token(
        app_session,
        tenant,
        member_id,
        expires_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1),
    )
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text


async def test_an_origin_not_on_the_allowlist_is_403(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "origin@example.com", "org_admin")
    token = await _api_key_token(
        app_session, tenant, member_id, allowed_origins=["https://claude.ai"]
    )
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://evil.example.com",
                },
            )
    assert r.status_code == 403


async def test_an_allowed_origin_passes_through(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "origin-ok@example.com", "org_admin")
    token = await _api_key_token(
        app_session, tenant, member_id, allowed_origins=["https://claude.ai"]
    )
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": f"Bearer {token}", "Origin": "https://claude.ai"},
            )
    assert r.status_code == 200, r.text


async def test_a_successful_call_updates_last_used_at(app_session: AppSessionFactory) -> None:
    tenant, token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        rows = (await db.execute(m.ApiKey.__table__.select())).fetchall()
        assert len(rows) == 1
        assert rows[0].last_used_at is not None


async def test_propose_then_apply_executes_the_operation(
    app_session: AppSessionFactory,
) -> None:
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        agent = m.Agent(
            tenant_id=tenant, department_id=uuid.uuid4(), name="Target", mission="old"
        )
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            propose = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_propose",
                        "arguments": {
                            "operations": [
                                {
                                    "type": "agent.mission.set",
                                    "agentId": str(agent_id),
                                    "mission": "new",
                                }
                            ]
                        },
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert propose.status_code == 200, propose.text

            proposal = json.loads(propose.json()["result"]["content"][0]["text"])
            assert proposal["status"] == "draft"

            apply_ = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_apply_proposal",
                        "arguments": {"proposalId": proposal["id"]},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert apply_.status_code == 200, apply_.text
            applied = json.loads(apply_.json()["result"]["content"][0]["text"])
            assert applied["status"] == "applied"

    async with app_session(tenant) as db:
        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        assert reloaded.mission == "new"


async def test_reject_transitions_the_proposal_without_applying_it(
    app_session: AppSessionFactory,
) -> None:
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        agent = m.Agent(
            tenant_id=tenant, department_id=uuid.uuid4(), name="Target", mission="unchanged"
        )
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:

            propose = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_propose",
                        "arguments": {
                            "operations": [
                                {
                                    "type": "agent.mission.set",
                                    "agentId": str(agent_id),
                                    "mission": "new",
                                }
                            ]
                        },
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            proposal = json.loads(propose.json()["result"]["content"][0]["text"])

            reject = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_reject_proposal",
                        "arguments": {"proposalId": proposal["id"]},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert reject.status_code == 200, reject.text
            rejected = json.loads(reject.json()["result"]["content"][0]["text"])
            assert rejected["status"] == "rejected"

    async with app_session(tenant) as db:
        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        assert reloaded.mission == "unchanged"


async def test_list_proposals_defaults_to_draft_and_review_reads_one_by_id(
    app_session: AppSessionFactory,
) -> None:
    tenant, token = await _admin_setup(app_session)
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Target")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:

            propose = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_propose",
                        "arguments": {
                            "operations": [
                                {
                                    "type": "agent.mission.set",
                                    "agentId": str(agent_id),
                                    "mission": "new",
                                }
                            ]
                        },
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            proposal = json.loads(propose.json()["result"]["content"][0]["text"])

            listed = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "copilot_list_proposals", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            listed_proposals = json.loads(listed.json()["result"]["content"][0]["text"])
            assert [p["id"] for p in listed_proposals] == [proposal["id"]]

            reviewed = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_review_proposal",
                        "arguments": {"proposalId": proposal["id"]},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            reviewed_proposal = json.loads(reviewed.json()["result"]["content"][0]["text"])
            assert reviewed_proposal["id"] == proposal["id"]


async def test_propose_requires_copilot_manage_not_just_view(
    app_session: AppSessionFactory,
) -> None:
    """`operator` holds `copilot:view` (via `_VIEW_EVERYTHING`) but not
    `copilot:manage` -- proposing/applying/rejecting must stay refused."""
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "viewer@example.com", "operator")
    token = await _api_key_token(app_session, tenant, member_id)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_propose",
                        "arguments": {"operations": []},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 403, r.text


async def test_operator_role_can_still_list_and_review_with_view_permission(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "viewer2@example.com", "operator")
    token = await _api_key_token(app_session, tenant, member_id)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "copilot_list_proposals", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text


async def test_a_bare_member_role_cannot_even_list_proposals(
    app_session: AppSessionFactory,
) -> None:
    """`member` holds only `copilot:use`, not `copilot:view` -- confirming the
    API-key gateway's minted `Principal` is exactly as powerful as the real,
    live permissions of the member who created the key, no more."""
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "bare@example.com", "member")
    token = await _api_key_token(app_session, tenant, member_id)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "copilot_list_proposals", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 403, r.text


async def test_a_bare_member_role_cannot_list_or_get_any_of_the_new_read_tools(
    app_session: AppSessionFactory,
) -> None:
    """The same `member`-role token-floor guarantee `copilot_list_proposals`
    already has (copilot:use, not copilot:view) must hold for all 8 new
    read-only discovery tools too -- a shared `_require(principal,
    perm(COPILOT, VIEW))` call is easy to typo per-tool; this closes that
    gap with one parametrized pass over every new tool name."""
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    member_id = await _member_with_role(app_session, tenant, "bare-reader@example.com", "member")
    token = await _api_key_token(app_session, tenant, member_id)
    app = create_app()
    tool_calls = [
        ("copilot_list_departments", {}),
        ("copilot_get_department", {"departmentId": str(uuid.uuid4())}),
        ("copilot_list_agents", {}),
        ("copilot_get_agent", {"agentId": str(uuid.uuid4())}),
        ("copilot_list_plugins", {}),
        ("copilot_get_plugin", {"pluginId": str(uuid.uuid4())}),
        ("copilot_list_integrations", {}),
        ("copilot_list_connection_tools", {"connectionName": "whatever"}),
    ]
    async with LifespanManager(app):
        async with _client(app) as c:
            for name, arguments in tool_calls:
                r = await c.post(
                    "/mcp/external",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert r.status_code == 403, f"{name}: {r.text}"


async def test_a_member_with_no_role_id_mints_the_member_role_token_floor(
    app_session: AppSessionFactory,
) -> None:
    """Every member starts with `role_id IS NULL` (no admin has touched the
    Role Builder for them yet) -- that is the case `_verify_api_key` must
    mint `MEMBER_ROLE` for, the same token floor `password_login` gives this
    same member, not the empty permission set an unrecognised role name (the
    bug this pins: a hardcoded `role=""`) resolves to. Calls `_verify_api_key`
    directly rather than through a tools/call round-trip: nothing this
    router's own `_require` checks (`copilot:manage`/`copilot:view`) is
    actually in `MEMBER_ROLE`'s grant (`copilot:use` only), so an HTTP-level
    assertion here would pass identically under the bug it is meant to catch."""
    tenant = uuid.uuid4()
    await _sole_organization(app_session, tenant)
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant,
            subject="no-role-row@example.com",
            subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, "oc8:local:no-role-row@example.com"),
            role_id=None,
        )
        db.add(member)
        await db.flush()
        member_id = member.id
    token = await _api_key_token(app_session, tenant, member_id)
    request = Request({"type": "http", "headers": []})
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    principal = await _verify_api_key(request, creds)
    assert principal.role == MEMBER_ROLE


async def test_disabling_the_owning_member_disables_every_key_they_hold(
    app_session: AppSessionFactory,
) -> None:
    """A key is exactly as powerful as its owner -- soft-deleting the member
    must invalidate the key even though the key row itself is still enabled."""
    tenant, token = await _admin_setup(app_session, subject="soon-gone@example.com")


    async with app_session(tenant) as db:
        member = (
            await db.execute(
                m.OrgMember.__table__.select().where(
                    m.OrgMember.subject == "soon-gone@example.com"
                )
            )
        ).fetchone()
        await db.execute(
            m.OrgMember.__table__.update()
            .where(m.OrgMember.id == member.id)
            .values(deleted_at=dt.datetime.now(tz=dt.UTC))
        )
        await db.commit()

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 401, r.text


async def test_tools_call_of_an_unknown_tool_is_404(app_session: AppSessionFactory) -> None:
    _tenant, token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "not_a_real_tool", "arguments": {}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 404, r.text


async def test_review_of_a_missing_proposal_id_is_a_tool_error_not_a_500(
    app_session: AppSessionFactory,
) -> None:
    _tenant, token = await _admin_setup(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/mcp/external",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "copilot_review_proposal",
                        "arguments": {"proposalId": str(uuid.uuid4())},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["isError"] is True
    assert "not found" in body["content"][0]["text"]
