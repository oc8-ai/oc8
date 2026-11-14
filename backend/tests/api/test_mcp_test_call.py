"""POST /mcp/connections/{id}/test-call: one real call of one declared tool,
with an error class a person can act on and no secret in what comes back."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.agent.http_tools import HttpToolSession
from oc8.auth import get_identity_provider
from oc8.db.engine import get_engine
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("allow_loopback_outbound")]

_SECRET = "sk-TESTCALL-SECRET"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def _send(self, status: int, body: str) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.startswith("/tickets/"):
            self._send(200, json.dumps({"id": self.path.rsplit("/", 1)[1]}))
        elif self.path == "/needs-key":
            self._send(401, f"key {self.headers.get('X-Api-Key')} is not valid")
        elif self.path == "/echo":
            self._send(200, f"you sent {self.headers.get('X-Api-Key')}")
        elif self.path == "/boom":
            self._send(500, "internal error")
        else:
            self._send(404, "nope")

    def do_POST(self) -> None:
        self._send(500, "token service down")


@pytest.fixture
def api_server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


_TOOLS = [
    {"name": "get_ticket", "method": "GET", "url_template": "/tickets/{id}", "param_schema": {}},
    {"name": "needs_key", "method": "GET", "url_template": "/needs-key", "param_schema": {}},
    {"name": "boom", "method": "GET", "url_template": "/boom", "param_schema": {}},
    {"name": "echo", "method": "GET", "url_template": "/echo", "param_schema": {}},
    {
        "name": "with_header",
        "method": "GET",
        "url_template": "/tickets/1",
        "param_schema": {},
        "param_locations": {"X-Note": "header"},
    },
]


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _conn(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    base_url: str,
    *,
    auth: dict[str, object] | None = None,
    transport: str = "manual_http",
) -> uuid.UUID:
    cfg: dict[str, object] = {"http_tools": _TOOLS, "env": {"HTTP_AUTH_SECRET": _SECRET}}
    if auth is not None:
        cfg["auth"] = auth
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name="tickets",
            server_url=base_url,
            transport=transport,
            scopes={"read": ["get_ticket"], "modify": []},
            config=cfg,
            connected=False,
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
        await db.commit()
    return conn_id


async def _call(
    tenant: uuid.UUID, conn_id: uuid.UUID, body: dict[str, object], role: str = "org_admin"
):
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                f"/api/v1/mcp/connections/{conn_id}/test-call", json=body, headers=_h(tenant, role)
            )


async def test_a_working_call(app_session: AppSessionFactory, api_server: str) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(app_session, tenant, api_server)
    r = await _call(tenant, conn_id, {"tool": "get_ticket", "arguments": {"id": "42"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == 200
    assert body["errorClass"] is None
    assert json.loads(body["excerpt"]) == {"id": "42"}
    assert body["durationMs"] >= 0


async def test_test_call_excerpt_has_no_secret(
    app_session: AppSessionFactory, api_server: str
) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(
        app_session,
        tenant,
        api_server,
        auth={"kind": "api_key", "in": "header", "name": "X-Api-Key"},
    )
    r = await _call(tenant, conn_id, {"tool": "needs_key", "arguments": {}})
    body = r.json()
    assert body["ok"] is False
    assert body["errorClass"] == "auth"
    assert body["status"] == 401
    assert "Check the login" in body["message"]
    assert _SECRET not in r.text


async def test_a_working_call_never_shows_the_secret(
    app_session: AppSessionFactory, api_server: str
) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(
        app_session,
        tenant,
        api_server,
        auth={"kind": "api_key", "in": "header", "name": "X-Api-Key"},
    )
    r = await _call(tenant, conn_id, {"tool": "echo", "arguments": {}})
    body = r.json()
    assert body["ok"] is True
    assert body["excerpt"] == "you sent [redacted]"
    assert _SECRET not in r.text


async def test_a_non_ascii_header_argument_is_an_api_error(
    app_session: AppSessionFactory, api_server: str
) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(app_session, tenant, api_server)
    r = await _call(tenant, conn_id, {"tool": "with_header", "arguments": {"X-Note": "Grüße"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["errorClass"] == "http"
    assert body["status"] is None


async def test_a_token_failure_is_reported_as_token(
    app_session: AppSessionFactory, api_server: str
) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(
        app_session,
        tenant,
        api_server,
        auth={
            "kind": "oauth2_client_credentials",
            "token_url": f"{api_server}/token",
            "client_id": "c",
        },
    )
    body = (await _call(tenant, conn_id, {"tool": "get_ticket", "arguments": {"id": "1"}})).json()
    assert body["errorClass"] == "token"
    assert body["ok"] is False


async def test_a_blocked_address(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(app_session, tenant, "http://10.0.0.7")
    body = (await _call(tenant, conn_id, {"tool": "boom", "arguments": {}})).json()
    assert body["errorClass"] == "blocked"
    # No timing: how long the refusal took (a DNS answer for an internal name
    # versus a slow NXDOMAIN) would tell internal names apart.
    assert body["durationMs"] == 0


async def test_an_unreachable_api(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(app_session, tenant, "http://127.0.0.1:9")
    body = (await _call(tenant, conn_id, {"tool": "boom", "arguments": {}})).json()
    assert body["errorClass"] == "unreachable"


async def test_an_api_error(app_session: AppSessionFactory, api_server: str) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(app_session, tenant, api_server)
    body = (await _call(tenant, conn_id, {"tool": "boom", "arguments": {}})).json()
    assert body["errorClass"] == "http"
    assert body["status"] == 500
    assert body["excerpt"] == "internal error"


async def test_requires_plugin_manage(app_session: AppSessionFactory, api_server: str) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(app_session, tenant, api_server)
    r = await _call(tenant, conn_id, {"tool": "boom", "arguments": {}}, role="member")
    assert r.status_code == 403


async def test_another_organisations_connection_is_not_found(
    app_session: AppSessionFactory, api_server: str
) -> None:
    owner = uuid.uuid4()
    conn_id = await _conn(app_session, owner, api_server)
    r = await _call(uuid.uuid4(), conn_id, {"tool": "boom", "arguments": {}})
    assert r.status_code == 404


async def test_unknown_tool_missing_parameter_and_wrong_transport(
    app_session: AppSessionFactory, api_server: str
) -> None:
    tenant = uuid.uuid4()
    conn_id = await _conn(app_session, tenant, api_server)
    assert (await _call(tenant, conn_id, {"tool": "nope", "arguments": {}})).status_code == 404
    missing = await _call(tenant, conn_id, {"tool": "get_ticket", "arguments": {}})
    assert missing.status_code == 422
    assert "missing required parameter" in missing.text
    stdio = await _conn(app_session, tenant, "", transport="stdio")
    assert (await _call(tenant, stdio, {"tool": "boom", "arguments": {}})).status_code == 409


async def test_no_database_connection_is_held_during_the_call(
    app_session: AppSessionFactory, api_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The call can take up to its timeout; the request's transaction must be
    # over by then, or a burst of test calls ties up the shared pool.
    checked_out: list[int] = []
    original = HttpToolSession.try_call

    async def spy(self: HttpToolSession, name: str, arguments: dict[str, object]):
        checked_out.append(get_engine().pool.checkedout())
        return await original(self, name, arguments)

    monkeypatch.setattr(HttpToolSession, "try_call", spy)
    tenant = uuid.uuid4()
    conn_id = await _conn(app_session, tenant, api_server)
    r = await _call(tenant, conn_id, {"tool": "get_ticket", "arguments": {"id": "1"}})
    assert r.status_code == 200, r.text
    assert checked_out == [0]
