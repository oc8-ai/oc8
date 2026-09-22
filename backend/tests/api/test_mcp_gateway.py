"""oc8 as the tool gateway: an MCP server an agent container talks to (§8.7 R2).

The container is configured with exactly one MCP server -- us -- and never learns
the real one exists. Credentials stay control-plane-side, and every call passes
the PEP, which is the only reason the >3000 € approval is enforceable at all.

Same principle as the LLM gateway: speak the protocol the harness already speaks.
"""

from __future__ import annotations

import base64
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

MCP = "/mcp"
FRAME = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True, "approval_eur": 3000}}}

# tests/api/test_mcp_gateway.py -> repo root -> plugins/claude_code_runtime.
# One test in this file (the end-to-end ask_user path) imports claude_code_runtime
# inside its body; the plugin is no longer on pytest's `pythonpath` (see
# backend/pyproject.toml), so it resolves its own package the same way the plugin's
# own tests do.
CLAUDE_CODE_ROOT = Path(__file__).resolve().parents[3] / "capas" / "claude_code_runtime"


def _evict() -> None:
    """Drop every cached `runtime`/`runtime.*` module from sys.modules.

    claude_code_runtime's package is called `runtime` after the restructure --
    the folder convention shared by all four runtime_adapter plugins (design
    §2) -- and sys.modules is keyed by NAME, not by path. Called SYMMETRICALLY
    on both sides of the fixture's yield: before, so a sibling runtime's cached
    copy cannot answer our import; after, so nothing generic is left cached for
    anyone else, since `loader.import_entry_point` only evicts modules IT
    ITSELF introduced.
    """
    for _stale in [n for n in sys.modules if n == "runtime" or n.startswith("runtime.")]:
        del sys.modules[_stale]


@pytest.fixture()
def _plugin_path() -> Iterator[None]:
    """Same path-insert + sys.modules eviction as the plugin test modules use, but
    NOT autouse: exactly one test in this large, otherwise plugin-free file needs it,
    and an autouse fixture would evict and re-import the plugin for every test here.
    Requested by name as an argument instead, which also fixes the ordering."""
    _evict()
    sys.path.insert(0, str(CLAUDE_CODE_ROOT))
    yield
    sys.path.remove(str(CLAUDE_CODE_ROOT))
    _evict()


class _FakeMcp:
    """Stands in for the real MCP server the gateway forwards to.

    Keyed by the launch command, because with more than one connection the point
    of a test is that two DIFFERENT servers are reached -- a fake that answers
    identically whoever called it could not tell a routing bug from a pass.
    """

    calls: list[tuple[str, dict[str, Any]]] = []
    tools: list[Any] = []
    #: command -> tool names. Anything unlisted gets the default three.
    by_command: dict[str, list[str]] = {}
    #: (command, tool) in call order, so a test can assert WHICH server ran it.
    routed: list[tuple[str, str]] = []

    def __init__(self, command: str = "", *a: Any, **kw: Any) -> None:
        self._command = command

    async def __aenter__(self) -> _FakeMcp:
        from oc8.modelrouter import NeutralTool

        names = type(self).by_command.get(
            self._command, ["search_records", "create_record", "send_email"]
        )
        self.tools = [
            NeutralTool(name=n, description=n, parameters={"type": "object"}) for n in names
        ]
        type(self).tools = self.tools
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        type(self).calls.append((name, arguments))
        type(self).routed.append((self._command, name))
        return f"ok:{name}:{len(type(self).calls)}"


@pytest.fixture(autouse=True)
async def _no_session_leaks_between_tests() -> Any:
    """The gateway keeps a bridge's session open across calls now, which is the
    whole point -- but a fake left behind by one test must not serve the next."""
    from oc8.agent import mcp_pool

    await mcp_pool.close_all()
    yield
    await mcp_pool.close_all()


async def _seed(
    db: Any,
    tenant: uuid.UUID,
    *,
    frame: dict[str, Any] | None = None,
    config_extra: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    dept = m.Department(
        tenant_id=tenant, name="Vertrieb", frame=frame if frame is not None else FRAME
    )
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    conn = m.McpConnection(
        tenant_id=tenant,
        department_id=dept.id,
        name="odoo",
        transport="stdio",
        server_url="stdio://odoo",
        connected=True,
        # Mirrors the real Odoo connection: the core assumes NO value anywhere, so
        # a threshold only exists because the connection's plugin declares where
        # the value lives.
        config={
            "command": "x",
            "args": [],
            "value_spec": {
                "direct_fields": ["amount_total", "expected_revenue"],
                "line_items": {
                    "path": ["values", "order_line"],
                    "qty_field": "product_uom_qty",
                    "price_field": "price_unit",
                },
            },
        },
        scopes={"read": ["search_records"], "write": ["create_record"], "send": ["send_email"]},
    )
    if config_extra:
        conn.config = {**conn.config, **config_extra}
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Angebot",
        state="in_progress",
    )
    db.add_all([conn, task])
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        state="running",
        context={"task": "x", "mcp_connection_id": str(conn.id)},
    )
    db.add(run)
    await db.flush()
    return agent.id, run.id, task.id


def _token(tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return get_identity_provider().mint(
        tenant_id=tenant,
        subject=f"agent:{agent_id}",
        role="agent_default",
        kind="agent",
        scopes=[f"run:{run_id}"],
    )


async def _rpc(
    token: str | None, method: str, params: dict[str, Any] | None = None, *, rpc_id: int | None = 1
) -> tuple[int, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if rpc_id is not None:
        body["id"] = rpc_id
    if params is not None:
        body["params"] = params
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(MCP, json=body, headers=headers)
            return r.status_code, (r.json() if r.content else None)


# --------------------------------------------------------------------- auth


async def test_no_token_is_refused(app_session: AppSessionFactory) -> None:
    code, _ = await _rpc(None, "tools/list")
    assert code in (401, 403)


async def test_an_operator_token_is_refused(app_session: AppSessionFactory) -> None:
    """Only an agent shell may drive tools. Otherwise this endpoint would be a way
    to act on a tenant's systems while bypassing the operator API's own checks."""
    tenant = uuid.uuid4()
    op = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    code, _ = await _rpc(op, "tools/list")
    assert code == 403


# ----------------------------------------------------------------- handshake


async def test_initialize_answers_the_handshake(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness refuses to proceed without a protocol version and a tools
    capability, so getting this wrong means it never even lists tools."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x"}},
    )
    assert code == 200, body
    result = body["result"]
    assert result["protocolVersion"]
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"]


async def test_a_notification_gets_no_body(app_session: AppSessionFactory) -> None:
    """JSON-RPC notifications carry no id and MUST NOT be answered with a result;
    a harness treats an unexpected response as a protocol error."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id), "notifications/initialized", {}, rpc_id=None
    )
    assert code in (202, 204)
    assert not body


# --------------------------------------------------------------- tools/list


async def test_tools_list_advertises_the_connections_tools(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert code == 200, body
    names = [t["name"] for t in body["result"]["tools"]]
    assert "search_records" in names
    assert "create_record" in names
    # MCP names the schema inputSchema.
    assert body["result"]["tools"][0]["inputSchema"]["type"] == "object"


async def test_tools_list_hides_what_the_frame_forbids(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advertising a tool every call of which would be denied only invites the
    model to waste turns on it -- the same reasoning as withholding delegate_task
    from a non-lead."""
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    read_only = {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}}
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=read_only)

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert code == 200, body
    names = [t["name"] for t in body["result"]["tools"]]
    # Only the CONNECTION's tools are frame-filtered. request_decision is not one
    # of them -- it writes a request for a human inside oc8 and touches nothing
    # outside it, so a read-only agent must still be able to escalate rather than
    # go quiet about something a person needs to decide.
    core = {
        "request_decision",
        "search_knowledge",
        "search_memory",
        "memory_write",
        "ask_user",
        "render_component",
    }
    assert [n for n in names if n not in core] == ["search_records"], names
    assert core <= set(names)


# ------------------------------------------------ a connection whose env expires
#
# A `secret_env` ref of the form `oauth:...` is minted per launch from the
# connection's OAuthConnection (agent/mcp_env.py). That makes building the
# environment a step that can FAIL -- and one that must not be paid once and
# then cached for half an hour.


async def _oauth_connection(db: Any, tenant: uuid.UUID) -> uuid.UUID:
    from oc8.oauth.provisioning import provision_oauth_connection

    conn = await provision_oauth_connection(
        db,
        tenant_id=tenant,
        provider="microsoft",
        values={
            "azure_tenant_id": "tenant-guid",
            "client_id": "app-client-id",
            "client_secret": "shh",
        },
    )
    return conn.id


@pytest.fixture
def _graph(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A KEK for the secret store and a Microsoft token endpoint that answers."""
    import httpx

    from oc8.config import get_settings
    from oc8.oauth import http as oauth_http

    monkeypatch.setenv("OC8_SECRET_KEK", base64.b64encode(bytes(range(32))).decode())
    get_settings.cache_clear()
    oauth_http.set_transport_override(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"access_token": "at1", "expires_in": 3600})
        )
    )
    yield
    oauth_http.set_transport_override(None)
    get_settings.cache_clear()


async def test_an_oauth_backed_connection_is_not_left_pooled(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, _graph: None
) -> None:
    """The session must not outlive the token it was launched with.

    A pooled bridge keeps the environment it started with for up to MAX_AGE,
    and a Graph token can arrive with a minute of life left -- so every later
    call would answer 401, and silently: an error RESULT is not an exception,
    so nothing in the pool would evict it.
    """
    from oc8.agent import mcp_pool

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        oauth_id = await _oauth_connection(db, tenant)
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            config_extra={
                "secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"},
                "oauth_connection_id": str(oauth_id),
            },
        )

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "search_records", "arguments": {"model": "crm.lead"}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is False
    assert _FakeMcp.calls[0][0] == "search_records"
    assert not mcp_pool._LIVE, "a session launched with a minted token must not be kept"


async def test_a_connection_whose_token_cannot_be_minted_does_not_blank_tools_list(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving the environment is now something that can fail on its own.

    It used to happen OUTSIDE the guard that exists so one broken system cannot
    take the whole toolset with it -- the guard whose own comment records an
    agent losing its memory, its knowledge and every skill because of one
    unreachable server. An Azure blip must cost the agent Microsoft 365 and
    nothing else.
    """
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # An oauth: ref and no oauth_connection_id: the setup form was never
        # finished, so minting cannot even be attempted.
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            config_extra={"secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"}},
        )

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert code == 200, body
    assert "error" not in body, "one connection's failure is not a protocol failure"
    names = [t["name"] for t in body["result"]["tools"]]
    assert "search_records" not in names, "the connection that cannot start offers nothing"
    assert names, "and everything that has nothing to do with it is still there"


async def test_a_call_on_a_connection_whose_token_cannot_be_minted_is_a_tool_error(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same placement question on the call path: the model gets a readable
    error, not a 500 out of the gateway."""
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            config_extra={"secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"}},
        )

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "search_records", "arguments": {"model": "crm.lead"}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is True
    assert "oauth_connection_id" in body["result"]["content"][0]["text"]


# --------------------------------------------------------------- tools/call


async def test_an_allowed_call_is_forwarded(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "search_records", "arguments": {"model": "crm.lead"}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is False
    assert "ok:search_records" in body["result"]["content"][0]["text"]
    assert _FakeMcp.calls[0][0] == "search_records"


async def test_a_denied_call_is_a_readable_tool_error_not_a_transport_error(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DENY must come back as a tool RESULT with isError, so the model reads the
    reason and adapts. A JSON-RPC error would look like a broken server and the
    harness would retry or abort instead."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    read_only = {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}}
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=read_only)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": {"model": "sale.order"}},
    )
    assert code == 200, body
    assert "error" not in body, "a policy decision is not a protocol failure"
    assert body["result"]["isError"] is True
    assert _FakeMcp.calls == [], "nothing may reach the tool server"


async def test_a_repeated_write_is_idempotent(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    tok = _token(tenant, agent_id, run_id)
    args = {"name": "create_record", "arguments": {"model": "sale.order", "qty": 20}}
    await _rpc(tok, "tools/call", args)
    _, second = await _rpc(tok, "tools/call", args)

    assert len([c for c in _FakeMcp.calls if c[0] == "create_record"]) == 1
    assert "replay" in second["result"]["content"][0]["text"].lower()


async def test_a_second_message_to_the_same_record_never_reaches_the_server(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live, 2026-07-28: one ticket, one question, two full answers to the
    customer in a single run. Idempotency cannot catch it -- the bodies differ,
    so they are two different calls -- and the policy engine says yes to both,
    because the agent IS allowed to send. What makes the second one wrong is the
    recipient, so that is what the guard keys on."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    may_send = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}}
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            frame=may_send,
            config_extra={
                "outward_tools": ["send_email"],
                "focus_spec": {"entity_field": "model", "id_fields": ["record_id"]},
            },
        )

    tok = _token(tenant, agent_id, run_id)
    _, first = await _rpc(
        tok,
        "tools/call",
        {
            "name": "send_email",
            "arguments": {"model": "crm.lead", "record_id": 7, "body": "Guten Tag ..."},
        },
    )
    _, second = await _rpc(
        tok,
        "tools/call",
        {
            "name": "send_email",
            "arguments": {"model": "crm.lead", "record_id": 7, "body": "Nachtrag ..."},
        },
    )

    assert first["result"]["isError"] is False
    assert second["result"]["isError"] is True
    assert len([c for c in _FakeMcp.calls if c[0] == "send_email"]) == 1
    text = second["result"]["content"][0]["text"]
    assert "already sent" in text and "crm.lead#7" in text
    assert "retry" in text, "a bare denial reads as a transport failure"


async def test_a_message_to_a_different_record_is_still_allowed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One message per RECIPIENT, not one per run: a run that legitimately works
    two records must be able to answer both."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    may_send = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}}
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            frame=may_send,
            config_extra={
                "outward_tools": ["send_email"],
                "focus_spec": {"entity_field": "model", "id_fields": ["record_id"]},
            },
        )

    tok = _token(tenant, agent_id, run_id)
    await _rpc(
        tok,
        "tools/call",
        {
            "name": "send_email",
            "arguments": {"model": "crm.lead", "record_id": 7, "body": "a"},
        },
    )
    _, other = await _rpc(
        tok,
        "tools/call",
        {
            "name": "send_email",
            "arguments": {"model": "crm.lead", "record_id": 8, "body": "b"},
        },
    )
    assert other["result"]["isError"] is False
    assert len([c for c in _FakeMcp.calls if c[0] == "send_email"]) == 2


async def test_a_connection_that_declares_no_outward_tool_is_unguarded(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing changes for a connection that has not thought about this."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    may_send = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}}
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=may_send)

    tok = _token(tenant, agent_id, run_id)
    for body in ("a", "b"):
        _, res = await _rpc(
            tok,
            "tools/call",
            {
                "name": "send_email",
                "arguments": {"model": "crm.lead", "record_id": 7, "body": body},
            },
        )
        assert res["result"]["isError"] is False
    assert len([c for c in _FakeMcp.calls if c[0] == "send_email"]) == 2


# ------------------------------------------------------- more than one system


async def _second_connection(
    db: Any,
    tenant: uuid.UUID,
    dept_id: uuid.UUID,
    *,
    name: str,
    command: str,
    tools: list[str],
    scopes: dict[str, list[str]] | None = None,
) -> None:
    db.add(
        m.McpConnection(
            tenant_id=tenant,
            department_id=dept_id,
            name=name,
            transport="stdio",
            server_url=f"stdio://{name}",
            connected=True,
            config={"command": command, "args": []},
            scopes=scopes or {"read": [tools[0]], "write": tools[1:]},
        )
    )
    _FakeMcp.by_command[command] = tools
    await db.flush()


async def test_both_systems_tools_are_offered(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime used to carry ONE mcp_connection_id and, failing that, took
    the department's oldest connected system with a literal .limit(1). An agent
    set up with two systems saw one of them, silently."""
    _FakeMcp.by_command = {}
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {
        "tools": {
            "odoo": {"enabled": True, "read": True, "modify": True},
            "gitea": {"enabled": True, "read": True, "modify": True},
        }
    }
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=frame)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await _second_connection(
            db,
            tenant,
            agent.department_id,
            name="gitea",
            command="gitea-mcp",
            tools=["list_issues", "create_issue"],
        )
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {k: v for k, v in run.context.items() if k != "mcp_connection_id"}
        await db.commit()

    _, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    names = {t["name"] for t in body["result"]["tools"]}
    assert {"search_records", "create_record"} <= names, "the first system is still there"
    assert {"list_issues", "create_issue"} <= names, "and so is the second"


async def test_two_pinned_logins_are_both_offered(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logins are tenant-global (no department_id), so they never appear in
    the department fallback list. Pins have to travel as `mcp_connection_ids`;
    a single `mcp_connection_id` hid the second login for the whole run."""
    _FakeMcp.by_command = {}
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {
        "tools": {
            "odoo": {"enabled": True, "read": True, "modify": True},
            "gitea": {"enabled": True, "read": True, "modify": True},
        }
    }
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=frame)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        # Drop the department-scoped seed connection: a login is not one.
        seeded = (
            (await db.execute(select(m.McpConnection).where(m.McpConnection.tenant_id == tenant)))
            .scalars()
            .all()
        )
        for row in seeded:
            await db.delete(row)
        ids: list[str] = []
        for name, command, tools in (
            ("odoo", "odoo-login", ["search_records", "create_record"]),
            ("gitea", "gitea-login", ["list_issues", "create_issue"]),
        ):
            cred = m.Credential(
                tenant_id=tenant, name=f"{name}-login", credential_type="odoo_login"
            )
            db.add(cred)
            await db.flush()
            conn = m.McpConnection(
                tenant_id=tenant,
                name=name,
                transport="stdio",
                server_url=f"stdio://{name}",
                connected=True,
                credential_id=cred.id,
                config={"command": command, "args": []},
                scopes={"read": [tools[0]], "write": tools[1:]},
            )
            db.add(conn)
            await db.flush()
            ids.append(str(conn.id))
            _FakeMcp.by_command[command] = tools
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        ctx = {k: v for k, v in run.context.items() if k != "mcp_connection_id"}
        ctx["mcp_connection_ids"] = ids
        run.context = ctx
        await db.commit()

    _, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    names = {t["name"] for t in body["result"]["tools"]}
    assert {"search_records", "create_record"} <= names
    assert {"list_issues", "create_issue"} <= names


async def test_a_call_reaches_the_system_that_owns_the_tool(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this prevents is not a crash but a write against the WRONG
    software."""
    _FakeMcp.by_command = {}
    _FakeMcp.routed = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {
        "tools": {
            "odoo": {"enabled": True, "read": True, "modify": True},
            "gitea": {"enabled": True, "read": True, "modify": True},
        }
    }
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=frame)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await _second_connection(
            db,
            tenant,
            agent.department_id,
            name="gitea",
            command="gitea-mcp",
            tools=["list_issues", "create_issue"],
        )
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {k: v for k, v in run.context.items() if k != "mcp_connection_id"}
        await db.commit()

    tok = _token(tenant, agent_id, run_id)
    await _rpc(tok, "tools/list")
    await _rpc(tok, "tools/call", {"name": "create_issue", "arguments": {"title": "Bug"}})
    await _rpc(tok, "tools/call", {"name": "search_records", "arguments": {"model": "crm.lead"}})

    assert ("gitea-mcp", "create_issue") in _FakeMcp.routed
    assert ("x", "search_records") in _FakeMcp.routed
    assert not [c for c, t in _FakeMcp.routed if c == "gitea-mcp" and t == "search_records"]


async def test_a_name_both_systems_offer_is_only_callable_qualified(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the bare name gone, the model cannot express the ambiguous call, so
    it cannot silently write to the wrong system."""
    _FakeMcp.by_command = {}
    _FakeMcp.routed = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {
        "tools": {
            "odoo": {"enabled": True, "read": True, "modify": True},
            "gitea": {"enabled": True, "read": True, "modify": True},
        }
    }
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=frame)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await _second_connection(
            db,
            tenant,
            agent.department_id,
            name="gitea",
            command="gitea-mcp",
            tools=["search_records", "create_issue"],
            scopes={"read": ["search_records"], "write": ["create_issue"]},
        )
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {k: v for k, v in run.context.items() if k != "mcp_connection_id"}
        await db.commit()

    tok = _token(tenant, agent_id, run_id)
    _, listed = await _rpc(tok, "tools/list")
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "search_records" not in names
    assert {"odoo.search_records", "gitea.search_records"} <= names

    await _rpc(tok, "tools/call", {"name": "gitea.search_records", "arguments": {}})
    assert ("gitea-mcp", "search_records") in _FakeMcp.routed
    assert not [c for c, t in _FakeMcp.routed if c == "x"]


async def test_the_frame_can_grant_one_system_and_withhold_the_other(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is per connection. Two systems must not mean all-or-nothing."""
    _FakeMcp.by_command = {}
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {
        "tools": {
            "odoo": {"enabled": True, "read": True, "modify": True},
            "gitea": {"enabled": False},
        }
    }
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=frame)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await _second_connection(
            db,
            tenant,
            agent.department_id,
            name="gitea",
            command="gitea-mcp",
            tools=["list_issues", "create_issue"],
        )
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {k: v for k, v in run.context.items() if k != "mcp_connection_id"}
        await db.commit()

    _, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    names = {t["name"] for t in body["result"]["tools"]}
    assert "search_records" in names
    assert "create_issue" not in names


async def test_a_call_over_the_threshold_suspends_for_approval(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour the product is sold on. The model must be told the run is
    parked, and no action may reach the tool server."""
    from sqlalchemy import select

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {
            "name": "create_record",
            "arguments": {"model": "sale.order", "values": {"amount_total": 3900}},
        },
    )
    assert code == 200, body
    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"].lower()
    assert "approval" in text or "freigabe" in text
    assert _FakeMcp.calls == [], "the held action must not run"

    async with app_session(tenant) as db:
        ar = (
            await db.execute(select(m.ApprovalRequest).where(m.ApprovalRequest.tenant_id == tenant))
        ).scalar_one()
        assert ar.status == "pending"
        assert ar.action_type == "tool_send"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        # The verdict the runtime reads to park the run.
        assert run.context["isolated_result"]["status"] == "waiting_for_approval"


async def test_an_unknown_method_is_a_jsonrpc_error(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/nonsense")
    assert code == 200, body
    assert body["error"]["code"] == -32601


async def test_a_held_call_returns_its_readable_error_immediately_without_waiting(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the CURRENT contract: a call that needs approval parks the run and
    returns its readable tool error right away, with no bounded wait for an
    operator inside the request.

    This replaces `test_an_operator_deciding_during_the_wait_lets_the_call_through`,
    which pinned the OPPOSITE contract -- a short (~45-60 s) wait so an operator
    who was already watching could let the call complete inside the same request,
    saving a container restart. That optimisation could never fire in practice:
    the runtime adapter (plugins/nanoclaw_runtime/runtime/runtime.py)
    polls for the park marker every ~2 s and tears the container down as soon as
    it sees one, long before any human decides. Live testing (three approval
    cycles, each approved after 60+ s) showed the wait actively corrupting the
    run: the gateway held the call while the container it belonged to was
    destroyed out from under it, so the harness never got a tool_result, its
    transcript ended in a dangling tool_use, and the model endpoint rejected the
    resumed transcript outright (400). Returning immediately is what lets the
    harness record a proper tool_result and keeps the transcript resumable. See
    docs/superpowers/specs/2026-07-25-tool-gateway-design.md, Decision 1,
    "Reversed 2026-07-27".
    """
    import time

    from sqlalchemy import select

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, task_id = await _seed(db, tenant)

    started = time.monotonic()
    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {
            "name": "create_record",
            "arguments": {"model": "sale.order", "values": {"amount_total": 4200}},
        },
    )
    elapsed = time.monotonic() - started

    assert code == 200, body
    assert elapsed < 5, "the gateway must not block waiting for an operator"
    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"].lower()
    assert "approval" in text or "freigabe" in text
    assert _FakeMcp.calls == [], "the held action must not run before a decision"

    async with app_session(tenant) as db:
        ar = (
            await db.execute(select(m.ApprovalRequest).where(m.ApprovalRequest.tenant_id == tenant))
        ).scalar_one()
        assert ar.status == "pending"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["isolated_result"]["status"] == "waiting_for_approval"
        rows = (
            (await db.execute(select(m.ToolInvocation).where(m.ToolInvocation.task_id == task_id)))
            .scalars()
            .all()
        )
        assert rows == [], "nothing is recorded as invoked until a decision runs it"


async def _assign_skill(db: Any, tenant: uuid.UUID, agent_id: uuid.UUID) -> uuid.UUID:
    skill = m.Skill(tenant_id=tenant, name="Ticket-Erstantwort", description="Erstantwort")
    db.add(skill)
    await db.flush()
    version = m.SkillVersion(
        tenant_id=tenant,
        skill_id=skill.id,
        semver="1.0.0",
        definition={
            "schema_version": 1,
            "slug": "ticket-erstantwort",
            "version": "1.0.0",
            "instruction": "Sprich den Kunden mit Namen an.",
            "requires": {"tools": [], "kbs": []},
            "guardrails": ["Sage niemals eine Erstattung zu."],
        },
        artifact_hash=b"x",
    )
    db.add(version)
    await db.flush()
    skill.current_version_id = version.id
    db.add(
        m.SkillAssignment(
            tenant_id=tenant, agent_id=agent_id, skill_version_id=version.id, enabled=True
        )
    )
    await db.flush()
    return version.id


async def test_an_assigned_skill_is_offered_as_a_tool(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill the agent cannot call is a guardrail that never applies.

    Skills activate on demand: the agent gets a catalogue line and is told to
    call the named tool to load the procedure. Through this gateway that tool was
    never advertised, so a container-run agent could not load it -- and the
    guardrails inside it ("never promise a refund") were decorative. Observed
    live: the helpdesk agent promised a customer a refund, and `active_skill_ids`
    on the run was empty because there had been nothing to call.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        await _assign_skill(db, tenant, agent_id)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert code == 200, body
    names = [t["name"] for t in body["result"]["tools"]]
    assert "search_records" in names, "the connection's tools must still be there"
    assert any(n.startswith("skill_") for n in names), f"no skill tool offered: {names}"


async def test_calling_a_skill_tool_returns_its_procedure_and_records_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activation has to reach the run, or a later call cannot know the skill's
    guardrails are in force."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        version_id = await _assign_skill(db, tenant, agent_id)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)

    _FakeMcp.calls = []
    tok = _token(tenant, agent_id, run_id)
    _c, listed = await _rpc(tok, "tools/list")
    skill_tool = next(
        t["name"] for t in listed["result"]["tools"] if t["name"].startswith("skill_")
    )

    code, body = await _rpc(tok, "tools/call", {"name": skill_tool, "arguments": {}})
    assert code == 200, body
    text = body["result"]["content"][0]["text"]
    assert "Sprich den Kunden mit Namen an." in text
    assert "Erstattung" in text, "the guardrails must come with the procedure"
    assert _FakeMcp.calls == [], "a skill tool is ours, not the connection's"

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert str(version_id) in [str(s) for s in run.context.get("active_skill_ids", [])]


async def test_request_decision_is_offered_and_works_through_the_gateway(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container-run agent must be able to hand a decision to a human.

    request_decision records and returns -- the run carries on -- so refusing
    it here would mean the only agents that can ask a human are the ones NOT
    running in a container, i.e. not the ones actually doing the work.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, task_id = await _seed(db, tenant)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tok = _token(tenant, agent_id, run_id)

    _c, listed = await _rpc(tok, "tools/list")
    assert "request_decision" in [t["name"] for t in listed["result"]["tools"]]

    code, body = await _rpc(
        tok,
        "tools/call",
        {
            "name": "request_decision",
            "arguments": {
                "question": "Erstattung freigeben?",
                "context": "Ticket #42, 249 EUR doppelt abgebucht.",
                "options": [{"key": "full", "label": "Voll erstatten"}],
            },
        },
    )
    assert code == 200, body
    assert body["result"].get("isError") is not True
    assert "inbox" in body["result"]["content"][0]["text"].lower()

    async with app_session(tenant) as db:
        row = (await db.execute(m.ApprovalRequest.__table__.select())).fetchone()
        assert row is not None
        assert row.action_type == "decision"
        assert row.task_id == task_id, "the approval points back at the work it came from"


async def test_ask_user_is_offered_through_the_gateway(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container-driven runtime discovers what it may call from tools/list --
    it does not know ask_user exists unless its schema is advertised there.
    ask_user used to be withheld as a lifecycle tool MCP had no vocabulary for
    (see the old test this replaces); it is not withheld any more because it
    now has real MCP semantics of its own: park the run, resume on answer,
    same shape as an approval."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tok = _token(tenant, agent_id, run_id)

    _c, listed = await _rpc(tok, "tools/list")
    assert "ask_user" in [t["name"] for t in listed["result"]["tools"]]


async def test_ask_user_is_withheld_from_the_tenant_assistant_through_the_gateway(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This gateway's tool list is a SEPARATE hand-built list from
    control_tools.offered_tools -- a container-driven run of the Assistant
    (dormant today, no runtime_ref is ever set, but not impossible) must not
    regain ask_user just because it took the other runtime path. Same
    reasoning as offered_tools: Telegram free text has no way to answer a
    park at all."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        agent.is_tenant_assistant = True
        agent.is_team_lead = True
        await db.flush()
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tok = _token(tenant, agent_id, run_id)

    _c, listed = await _rpc(tok, "tools/list")
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "ask_user" not in names
    assert "delegate_task" in names, "the withholding must be scoped to ask_user only"


async def test_search_knowledge_is_offered_through_the_gateway(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it a knowledge base granted to a container-run agent is
    unreachable: this runtime retrieves nothing at the start, because its task
    text is a trigger and says nothing about what to look up."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)

    _c, listed = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert "search_knowledge" in [t["name"] for t in listed["result"]["tools"]]


async def test_a_knowledge_lookup_with_no_grants_says_so_rather_than_failing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent with no knowledge base must get a plain "nothing here", not an
    error it will read as a broken tool and retry."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "search_knowledge", "arguments": {"query": "Erstattungsfrist"}},
    )
    assert code == 200, body
    assert body["result"].get("isError") is not True
    assert "nichts" in body["result"]["content"][0]["text"].lower()


async def test_render_component_is_offered_through_the_gateway(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)

    _c, listed = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert "render_component" in [t["name"] for t in listed["result"]["tools"]]


async def test_rendering_a_granted_component_publishes_a_realtime_event(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[tuple[str, dict[str, Any]]] = []

    class _SpyBus:
        async def publish_event(
            self, tenant_id: Any, type_: str, data: dict[str, Any], **kw: Any
        ) -> None:
            published.append((type_, data))

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _SpyBus())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent_id,
            )
        )
        await db.commit()

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {
            "name": "render_component",
            "arguments": {"component_key": "record_card", "props": {"title": "Acme"}},
        },
    )
    assert code == 200, body
    assert body["result"]["isError"] is False
    assert (
        "run.component_rendered",
        {
            "run_id": str(run_id),
            "component_key": "record_card",
            "props": {
                "title": "Acme",
                "subtitle": None,
                "fields": [],
                "link_label": None,
                "link_url": None,
            },
        },
    ) in published


async def test_rendering_an_ungranted_component_is_an_error_not_a_silent_no_op(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {
            "name": "render_component",
            "arguments": {"component_key": "record_card", "props": {"title": "Acme"}},
        },
    )
    assert code == 200, body
    assert body["result"]["isError"] is True
    assert "not been granted" in body["result"]["content"][0]["text"]


async def test_the_memory_tools_are_offered_and_the_tier_policy_still_applies(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading and asking are waved through -- they carry their own grant checks.
    WRITING memory is not: the tier policy is the whole protection there (§10.1),
    so a company-tier write must come back needing a human even though the same
    call from the same agent to the same gateway is otherwise allowed."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tok = _token(tenant, agent_id, run_id)

    _c, listed = await _rpc(tok, "tools/list")
    names = [t["name"] for t in listed["result"]["tools"]]
    assert {"search_memory", "memory_write"} <= set(names)

    code, body = await _rpc(
        tok,
        "tools/call",
        {
            "name": "memory_write",
            "arguments": {"tier": "company", "content": "Ab 1.9. neue Preisliste."},
        },
    )
    assert code == 200, body
    text = body["result"]["content"][0]["text"]
    assert "freigegeben" in text.lower() or "approv" in text.lower()

    async with app_session(tenant) as db:
        approval = (await db.execute(m.ApprovalRequest.__table__.select())).fetchone()
        assert approval is not None and approval.action_type == "memory_write"


async def test_a_frame_can_name_which_tools_a_system_offers(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling has always been about rights; measured here, it has to be about
    surface too. Connecting a second system took the advertised list from 14
    tools to 35 and the model stopped working -- it looped on one lookup instead
    of proceeding. Most of those were tools the department never uses: a bridge
    exposes everything its software can do, which is not everything an agent
    should see."""
    _FakeMcp.by_command = {}
    _FakeMcp.routed = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {
        "tools": {
            "odoo": {
                "enabled": True,
                "read": True,
                "modify": True,
                "modify": True,
                "only": ["search_records", "send_email"],
            }
        }
    }
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=frame)

    tok = _token(tenant, agent_id, run_id)
    _, listed = await _rpc(tok, "tools/list")
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"search_records", "send_email"} <= names
    assert "create_record" not in names


async def test_a_withheld_tool_is_refused_even_when_called_anyway(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hiding is not holding. A model that has seen the name once -- in an
    earlier run, or in its mission -- will call it regardless."""
    _FakeMcp.by_command = {}
    _FakeMcp.routed = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {
        "tools": {
            "odoo": {
                "enabled": True,
                "read": True,
                "modify": True,
                "modify": True,
                "only": ["search_records"],
            }
        }
    }
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame=frame)

    _, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": {"model": "sale.order"}},
    )
    assert body["result"]["isError"] is True
    assert not [c for c in _FakeMcp.calls if c[0] == "create_record"]


# ------------------------------------------- two agents, one queue, one record

FOCUS = {"entity_field": "model", "id_fields": ["record_id"], "search_tools": ["search_records"]}
MAY_WRITE = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}}


async def _colleague(
    db: Any, tenant: uuid.UUID, dept_id: uuid.UUID, conn_id: uuid.UUID, name: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """A second agent in the SAME department, with its own run."""
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept_id,
        name=name,
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept_id,
        assigned_agent_id=agent.id,
        title="Ticket",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        state="running",
        context={"task": "x", "mcp_connection_id": str(conn_id)},
    )
    db.add(run)
    await db.flush()
    return agent.id, run.id


async def test_the_second_agent_is_turned_away_from_a_record_being_worked(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents polling one queue are handed the same oldest item. They read it
    in the same second, so a mission rule ("claim it before you write") cannot
    close the race -- by the time either writes, both have decided to."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first_agent, first_run, _t = await _seed(
            db, tenant, frame=MAY_WRITE, config_extra={"focus_spec": FOCUS}
        )
        agent = await db.get(m.Agent, first_agent)
        assert agent is not None
        conn = (
            await db.execute(select(m.McpConnection).where(m.McpConnection.name == "odoo"))
        ).scalar_one()
        second_agent, second_run = await _colleague(db, tenant, agent.department_id, conn.id, "Jan")
        await db.commit()

    args = {"name": "create_record", "arguments": {"model": "helpdesk.ticket", "record_id": 77}}
    _, first = await _rpc(_token(tenant, first_agent, first_run), "tools/call", args)
    _, second = await _rpc(_token(tenant, second_agent, second_run), "tools/call", args)

    assert first["result"]["isError"] is False
    assert second["result"]["isError"] is True
    text = second["result"]["content"][0]["text"]
    assert "Nora" in text, "the second agent is told WHO has it"
    assert "next item" in text, "and what to do instead -- a bare denial gets retried"
    assert len([c for c in _FakeMcp.calls if c[0] == "create_record"]) == 1


async def test_the_holder_may_keep_working_its_own_record(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claiming a record must not lock the agent out of it: a run answers, then
    moves the ticket on."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db, tenant, frame=MAY_WRITE, config_extra={"focus_spec": FOCUS}
        )

    # Different calls on the SAME record -- answer the customer, then move the
    # ticket on. Repeating one identical call would be folded by idempotency and
    # would prove nothing about the claim.
    tok = _token(tenant, agent_id, run_id)
    for args in (
        {"model": "helpdesk.ticket", "record_id": 77, "body": "Guten Tag"},
        {"model": "helpdesk.ticket", "record_id": 77, "stage_id": 2},
        {"model": "helpdesk.ticket", "record_id": 77, "stage_id": 4},
    ):
        _, res = await _rpc(tok, "tools/call", {"name": "create_record", "arguments": args})
        assert res["result"]["isError"] is False
    assert len([c for c in _FakeMcp.calls if c[0] == "create_record"]) == 3


async def test_reading_a_record_someone_else_holds_is_fine(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents looking at the same queue is not a conflict; it is how a queue
    works. Only a CHANGE takes the record."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first_agent, first_run, _t = await _seed(
            db, tenant, frame=MAY_WRITE, config_extra={"focus_spec": FOCUS}
        )
        agent = await db.get(m.Agent, first_agent)
        assert agent is not None
        conn = (
            await db.execute(select(m.McpConnection).where(m.McpConnection.name == "odoo"))
        ).scalar_one()
        second_agent, second_run = await _colleague(db, tenant, agent.department_id, conn.id, "Jan")
        await db.commit()

    await _rpc(
        _token(tenant, first_agent, first_run),
        "tools/call",
        {
            "name": "create_record",
            "arguments": {"model": "helpdesk.ticket", "record_id": 77},
        },
    )
    _, read = await _rpc(
        _token(tenant, second_agent, second_run),
        "tools/call",
        {
            "name": "search_records",
            "arguments": {"model": "helpdesk.ticket"},
        },
    )
    assert read["result"]["isError"] is False


# ------------------------------------------------- how far one run may reach


async def test_a_run_is_stopped_when_it_reaches_past_its_limit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defence against prompt injection that actually holds.

    A ticket body is written by a stranger and lands in the model's context
    beside oc8's own instructions; there is no reliable way to tell an
    instruction from a quoted one. So the question is not how to detect the
    attack but how to survive being fooled -- "close every ticket" has to become
    "a handful mishandled, then a stop and a human who has been told".
    """
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {**MAY_WRITE, "limits": {"records_per_run": 3}}
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db, tenant, frame=frame, config_extra={"focus_spec": FOCUS}
        )

    tok = _token(tenant, agent_id, run_id)
    for record in (1, 2, 3):
        _, ok = await _rpc(
            tok,
            "tools/call",
            {
                "name": "create_record",
                "arguments": {"model": "helpdesk.ticket", "record_id": record},
            },
        )
        assert ok["result"]["isError"] is False, f"record {record} is within the limit"

    _, stopped = await _rpc(
        tok,
        "tools/call",
        {
            "name": "create_record",
            "arguments": {"model": "helpdesk.ticket", "record_id": 4},
        },
    )
    assert stopped["result"]["isError"] is True
    text = stopped["result"]["content"][0]["text"]
    assert "limit of 3" in text
    assert "do not try to work around" in text.lower()
    assert len([c for c in _FakeMcp.calls if c[0] == "create_record"]) == 3, (
        "the fourth record must never be reached"
    )

    async with app_session(tenant) as db:
        ar = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "blast_radius")
            )
        ).scalar_one()
        assert ar.status == "pending"
        assert ar.payload["touched"] == 3
        # Parked with the same marker the approval gate uses, so the adapter
        # tears the container down within seconds and a colleague who finds the
        # work legitimate can let it continue.
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["isolated_result"]["status"] == "waiting_for_approval"


async def test_working_one_record_many_times_never_hits_the_limit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The radius is how FAR a run reaches, not how much it does. An agent that
    answers, labels and closes one ticket must not be stopped halfway."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {**MAY_WRITE, "limits": {"records_per_run": 1}}
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db, tenant, frame=frame, config_extra={"focus_spec": FOCUS}
        )

    tok = _token(tenant, agent_id, run_id)
    for i in range(4):
        _, res = await _rpc(
            tok,
            "tools/call",
            {
                "name": "create_record",
                "arguments": {"model": "helpdesk.ticket", "record_id": 77, "step": i},
            },
        )
        assert res["result"]["isError"] is False
    assert len([c for c in _FakeMcp.calls if c[0] == "create_record"]) == 4


async def test_a_department_can_lift_the_limit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A department that legitimately works in batches says so in its frame.
    Zero means no ceiling -- deliberately explicit, so nobody removes the
    protection by forgetting to configure it."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    frame = {**MAY_WRITE, "limits": {"records_per_run": 0}}
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db, tenant, frame=frame, config_extra={"focus_spec": FOCUS}
        )

    tok = _token(tenant, agent_id, run_id)
    for record in range(1, 9):
        _, res = await _rpc(
            tok,
            "tools/call",
            {
                "name": "create_record",
                "arguments": {"model": "helpdesk.ticket", "record_id": record},
            },
        )
        assert res["result"]["isError"] is False
    assert len([c for c in _FakeMcp.calls if c[0] == "create_record"]) == 8


async def test_the_limit_holds_without_a_department_saying_anything(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforced by DEFAULT, not opt-in. A limit nobody switched on protects
    nobody, and the agent that gets attacked will be the one nobody configured."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db, tenant, frame=MAY_WRITE, config_extra={"focus_spec": FOCUS}
        )

    tok = _token(tenant, agent_id, run_id)
    results = []
    for record in range(1, 9):
        _, res = await _rpc(
            tok,
            "tools/call",
            {
                "name": "create_record",
                "arguments": {"model": "helpdesk.ticket", "record_id": record},
            },
        )
        results.append(res["result"]["isError"])
    from oc8.agent.blast_radius import DEFAULT_RECORDS_PER_RUN

    assert results.count(False) == DEFAULT_RECORDS_PER_RUN
    assert results[DEFAULT_RECORDS_PER_RUN] is True


async def test_a_team_lead_can_delegate_through_the_gateway(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delegate_task was refused here as a "lifecycle" tool. It is not one: it
    creates a run for SOMEBODY ELSE and returns a sentence -- the caller's run is
    untouched (that is ask_user, which stays refused).

    The cost of getting that wrong was total: a team lead could delegate only
    when NOT running in a container, which is the mode this system actually runs
    in. Observed live 2026-07-29 -- an accepted handoff put an order on a lead,
    and she "delegated" by writing the sub-tasks in prose, because the tool she
    had been told to call was not on her list.

    The sub-run is recorded on the run's context rather than published here: the
    gateway has not committed yet, and a stream entry whose row is not yet
    visible is a run a worker picks up and cannot find.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _task_id = await _seed(db, tenant)
        lead = await db.get(m.Agent, agent_id)
        assert lead is not None
        lead.is_team_lead = True
        mate = m.Agent(
            tenant_id=tenant,
            department_id=lead.department_id,
            name="Jan",
            status="idle",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add(mate)
        await db.flush()
        mate_id = mate.id
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tok = _token(tenant, agent_id, run_id)

    _c, listed = await _rpc(tok, "tools/list")
    assert "delegate_task" in [t["name"] for t in listed["result"]["tools"]]

    code, body = await _rpc(
        tok,
        "tools/call",
        {
            "name": "delegate_task",
            "arguments": {
                "agent_id": str(mate_id),
                "task_text": "Vertrag für Acme anlegen und den Kunden bestätigen.",
            },
        },
    )
    assert code == 200, body
    assert body["result"].get("isError") is not True
    assert "Jan" in body["result"]["content"][0]["text"]

    async with app_session(tenant) as db:
        sub = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == mate_id)))
            .scalars()
            .first()
        )
        assert sub is not None, "the colleague really has work, not just a nice answer"
        assert sub.source == "delegation"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert str(sub.id) in run.context.get("pending_runs", []), (
            "the runtime has to hand it to the executor, which publishes after commit"
        )


async def test_delegate_task_is_not_offered_to_an_agent_that_leads_nobody(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every call would be denied, so offering it only invites wasted turns --
    the same rule the connection tools are filtered by."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)

    _c, listed = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert "delegate_task" not in [t["name"] for t in listed["result"]["tools"]]


async def test_one_unreachable_system_does_not_take_the_whole_tool_list(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent's memory, its knowledge and its skills have nothing to do with
    whether a ticket system is up.

    They shared a fate anyway: discovery raised, the exception escaped
    tools/list, the bridge registered with ZERO tools, and the agent answered "no
    tool is available to me" -- which reads like a configuration mistake and is
    not one. Observed live 2026-07-30 with a stdio server that exits at startup.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    async def _dead(*_a: Any, **_k: Any) -> list[Any]:
        raise RuntimeError("Connection closed")

    monkeypatch.setattr("oc8.agent.mcp_pool.tools", _dead)

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert code == 200, body
    names = [t["name"] for t in body["result"]["tools"]]
    assert "search_memory" in names, "memory survives a system being down"
    assert "search_knowledge" in names
    assert "request_decision" in names, "and it can still reach a human"


async def test_ask_user_parks_the_run_with_a_marker_and_nothing_else(
    app_session: AppSessionFactory,
) -> None:
    """The behaviour a container-driven harness relies on: calling ask_user
    parks the run, so the plugin's poll loop sees the marker and tears the
    container down.

    And ONLY that. The Clarification row and the WAITING_FOR_INPUT transition
    are executor.py's job, when it sees the adapter's
    RunResult(status="waiting_for_input") -- see
    test_ask_user_through_a_plugin_creates_exactly_one_clarification for the
    full path. Doing either of them here as well double-books the park and
    raises on an illegal second transition; this test pins the gateway to the
    same shape its REQUIRE_APPROVAL sibling has (write a marker, nothing
    else)."""
    from sqlalchemy import select

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "ask_user", "arguments": {"question": "Which invoice number?"}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"].lower()
    assert "parked" in text or "operator" in text

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["isolated_result"]["status"] == "waiting_for_input"
        assert run.context["isolated_result"]["output"] == "Which invoice number?"
        # Untouched by the gateway: still whatever the executor left it as.
        assert run.state == "running"
        clars = (
            (await db.execute(select(m.Clarification).where(m.Clarification.run_id == run_id)))
            .scalars()
            .all()
        )
        assert clars == []


async def test_parking_a_run_keeps_a_session_id_committed_mid_request(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction of the race cli_harness/session_state.py closed.

    Both writers of `agent_run.context` are live at once: the plugin's poll
    loop commits its resume id from the executor's session, and this request
    writes the park marker from its own. The gateway's snapshot of the row is
    taken by `_caller()` at the top of the request and is several round trips
    old by the time it parks, so a `{**run.context, ...}` write here would
    silently revert a session id committed in between -- the next resume leg
    would then resume an older session or restart from scratch. Simulated by
    committing that session id from a SECOND session mid-request, while this
    one still holds its stale copy.
    """
    from cli_harness.session_state import set_session_id

    from oc8.skills.runtime import load_assigned_skills

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        # Pre-cached routes, so the only run-row write this request makes is
        # the park marker itself: `_remember_routes` would otherwise lock the
        # row before the interleaved session could commit.
        run.context = {**run.context, "tool_routes": {"create_record": ["odoo", "create_record"]}}

    interleaved = False

    async def _plugin_commits_first(db: Any, **kwargs: Any) -> Any:
        # Called by _call_tool AFTER the run row was loaded and BEFORE the park
        # marker is written -- exactly the window the poll loop writes in.
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            async with app_session(tenant) as plugin_db:
                plugin_run = await plugin_db.get(m.AgentRun, run_id)
                assert plugin_run is not None
                await set_session_id(plugin_db, plugin_run, "claude_code_runtime", "sess-live")
        return await load_assigned_skills(db, **kwargs)

    # The gateway imported the name into its own module, so that is where it
    # has to be replaced.
    monkeypatch.setattr("oc8.api.mcp_gateway.load_assigned_skills", _plugin_commits_first)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "ask_user", "arguments": {"question": "Which invoice number?"}},
    )
    assert code == 200, body
    assert interleaved, "the interleaved write never happened; the test proves nothing"
    assert body["result"]["isError"] is True

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["isolated_result"]["status"] == "waiting_for_input"
        # The whole point: the plugin's key survived the gateway's own write.
        assert run.context["cli_harness_sessions"]["claude_code_runtime"] == "sess-live"


async def test_ask_user_through_a_plugin_creates_exactly_one_clarification(
    _plugin_path: None,
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """The whole ask_user path, end to end, across the three files that own a
    piece of it -- which is where the bug lived that no single one of their
    own unit tests could see.

    A fake claude-code container calls ask_user over the REAL /mcp gateway
    mid-run; the gateway parks the run; claude_code_runtime's poll loop sees
    the marker and returns RunResult(status="waiting_for_input"); executor.py
    turns that into the Clarification row and the WAITING_FOR_INPUT
    transition. Before the fix the gateway ALSO transitioned the run itself,
    so executor.py's request_clarification() attempted
    WAITING_FOR_INPUT -> WAITING_FOR_INPUT, which runtime/states.py forbids:
    this test raised out of execute_run() rather than reaching a single
    assertion below."""
    from runtime.runtime import ClaudeCodeRuntime
    from sqlalchemy import select

    from oc8.runtime.executor import execute_run
    from oc8.runtime.queue import RunMessage
    from oc8.sandbox.types import ExecResult, SandboxHandle

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        # execute_run picks up a QUEUED run and makes it RUNNING itself.
        run.state = "queued"

    token = _token(tenant, agent_id, run_id)

    class _AsksOnFirstPoll:
        """Plays the container: its first logs() poll is the moment the
        harness calls ask_user, so the gateway call happens WHILE the poll
        loop is running, exactly as it does in production."""

        def __init__(self) -> None:
            self.asked = False
            self.specs: list[Any] = []
            self.torn_down = 0

        async def provision(self, spec: Any) -> SandboxHandle:
            self.specs.append(spec)
            return SandboxHandle(container_id="claude-container", image=spec.image)

        async def logs(self, handle: SandboxHandle) -> str:
            if not self.asked:
                self.asked = True
                code, body = await _rpc(
                    token,
                    "tools/call",
                    {"name": "ask_user", "arguments": {"question": "Which invoice number?"}},
                )
                assert code == 200, body
            return '{"type":"system","subtype":"init","session_id":"claude-sess-park"}\n'

        async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
            raise TimeoutError("still running")

        async def exec(self, handle: SandboxHandle, command: list[str], **kw: Any) -> ExecResult:
            raise NotImplementedError

        async def fs_write(self, handle: SandboxHandle, path: str, content: bytes) -> None:
            raise NotImplementedError

        async def fs_read(self, handle: SandboxHandle, path: str) -> bytes:
            raise NotImplementedError

        async def teardown(self, handle: SandboxHandle) -> None:
            self.torn_down += 1

        async def reap_orphans(self) -> int:
            return 0

    driver = _AsksOnFirstPoll()
    monkeypatch.setattr("runtime.runtime.get_sandbox_driver", lambda: driver)
    monkeypatch.setattr("runtime.runtime.SESSION_ROOT_OVERRIDE", str(tmp_path))

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=ClaudeCodeRuntime(),
    )

    assert driver.torn_down == 1
    async with app_session(tenant) as db:
        clars = (
            (await db.execute(select(m.Clarification).where(m.Clarification.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(clars) == 1, "exactly one -- not zero, and not one per park site"
        assert clars[0].status == "open"
        assert clars[0].question == "Which invoice number?"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == "waiting_for_input"
        assert run.context["pending_question"] == "Which invoice number?"


async def test_ask_user_with_an_empty_question_is_a_model_error_not_a_park(
    app_session: AppSessionFactory,
) -> None:
    """Mirrors control_tools.py's own guard: an empty question must not park
    a run for a human to stare at nothing."""
    from sqlalchemy import select

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "ask_user", "arguments": {"question": "   "}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is True
    assert "non-empty" in body["result"]["content"][0]["text"].lower()

    async with app_session(tenant) as db:
        count = (
            (await db.execute(select(m.Clarification).where(m.Clarification.run_id == run_id)))
            .scalars()
            .all()
        )
        assert count == []
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == "running"


async def test_a_connections_requirements_wrap_both_gateway_launch_sites(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`requirements` must survive the GATEWAY path, not just `McpSession(cfg…)`.

    The gateway launches through `mcp_pool`, which holds no `cfg` and therefore
    cannot wrap anything itself -- so both of its call sites (tools/list and
    tools/call) have to apply `wrap_with_requirements` before handing the pool a
    command. This is the path every packaged/containerized runtime uses; missed,
    a plugin's `uv run --with` overlay reaches the in-process engine and the
    "Test connection" button but not the actual run, so the button goes green
    and the agent dies on ModuleNotFoundError.
    """
    from oc8.agent import mcp_pool

    launched: list[tuple[str, list[str]]] = []
    wrapped = ("uv", ["run", "--with", "httpx>=0.27", "--", "python", "-m", "bridge"])

    class _RecordingMcp(_FakeMcp):
        def __init__(
            self, command: str = "", args: list[str] | None = None, *a: Any, **kw: Any
        ) -> None:
            launched.append((command, list(args or [])))
            super().__init__(command, *a, **kw)

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _RecordingMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            config_extra={
                "command": "python",
                "args": ["-m", "bridge"],
                "requirements": ["httpx>=0.27"],
            },
        )
    token = _token(tenant, agent_id, run_id)

    code, body = await _rpc(token, "tools/list")
    assert code == 200, body
    assert launched[-1] == wrapped, "tools/list launched the bridge unwrapped"

    # The pool caches on command/args, so drop the session to force tools/call
    # to launch its own -- otherwise it would reuse the one above and prove
    # nothing about the second call site.
    await mcp_pool.close_all()
    launched.clear()

    code, body = await _rpc(
        token, "tools/call", {"name": "search_records", "arguments": {"model": "crm.lead"}}
    )
    assert code == 200, body
    assert body["result"]["isError"] is False
    assert launched[-1] == wrapped, "tools/call launched the bridge unwrapped"


async def test_a_connection_without_requirements_is_launched_verbatim(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper is a no-op without `requirements` -- no shipped plugin
    declares any, so this is what every launch today must still look like."""
    launched: list[tuple[str, list[str]]] = []

    class _RecordingMcp(_FakeMcp):
        def __init__(
            self, command: str = "", args: list[str] | None = None, *a: Any, **kw: Any
        ) -> None:
            launched.append((command, list(args or [])))
            super().__init__(command, *a, **kw)

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _RecordingMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db, tenant, config_extra={"command": "python", "args": ["-m", "bridge"]}
        )

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert code == 200, body
    assert launched[-1] == ("python", ["-m", "bridge"])
