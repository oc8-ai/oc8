"""What an MCP bridge is handed at launch (agent/mcp_env.py).

A password sits in the secret store and is the same next month. A Microsoft
Graph access token lives about an hour, so storing one and resolving it like a
password hands the bridge an expired string on nearly every launch. The
`oauth:` prefix on a `secret_env` ref is how a manifest says "mint this fresh,
from the OAuth connection this was set up against" -- and the plain refs every
other plugin uses must keep behaving exactly as they did.
"""

from __future__ import annotations

import base64
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agent.mcp_env import has_oauth_ref, resolve_mcp_env
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result
from oc8.oauth import http as oauth_http
from oc8.oauth.provisioning import provision_oauth_connection
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

#: The root of the `google_workspace` plugin, whose stdio bridge is the package
#: `mcp_bridge`. Not on pytest's `pythonpath` (backend/pyproject.toml lists only
#: the runtime plugins), so the one test below that checks what the bridge DOES
#: with the environment this module resolves inserts it on `sys.path` itself --
#: same reasoning as plugins/google_workspace/tests/test_manifest.py. Resolved
#: at import time, not inside the async test, so the filesystem call is not made
#: on the event loop.
_GOOGLE_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "capas" / "google_workspace"


def _evict_generic_plugin_packages() -> None:
    """Drop every cached `connector`/`mcp_bridge` module from sys.modules.

    Both names are generic after the package restructure -- several plugins
    ship one -- and sys.modules is keyed by NAME, not by path. The one test
    below imports google_workspace's `mcp_bridge` directly, so it evicts on
    BOTH sides: before, so a sibling's cached copy cannot answer it; after, so
    nothing generic is left behind for `loader.import_entry_point` (which only
    evicts modules IT ITSELF introduced) to hand back to the wrong plugin.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
    ]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from oc8.config import get_settings

    monkeypatch.setenv("OC8_SECRET_KEK", base64.b64encode(bytes(range(32))).decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _graph_token() -> Iterator[None]:
    """Microsoft's token endpoint, answering every mint with a fresh token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})

    oauth_http.set_transport_override(httpx.MockTransport(handler))
    yield
    oauth_http.set_transport_override(None)


async def _oauth_connection(db: Any, tenant: uuid.UUID, client_id: str) -> m.OAuthConnection:
    return await provision_oauth_connection(
        db,
        tenant_id=tenant,
        provider="microsoft",
        values={
            "azure_tenant_id": "tenant-guid",
            "client_id": client_id,
            "client_secret": "shh",
        },
    )


async def test_only_an_oauth_backed_connection_declares_an_expiring_environment() -> None:
    """Asked by the session pool, which may hold a bridge open for half an hour:
    a stored password is the same in half an hour, a minted token is not."""
    assert has_oauth_ref({"secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"}})
    assert not has_oauth_ref({"secret_env": {"ODOO_PASSWORD": "odoo/password"}})
    assert not has_oauth_ref({"env": {"ODOO_URL": "https://odoo"}})
    assert not has_oauth_ref({})
    # One is enough -- the whole session is launched with the same environment.
    assert has_oauth_ref({"secret_env": {"A": "plain/ref", "B": "oauth:token"}})


async def test_a_plain_ref_still_goes_through_the_secret_store(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await store_secret(
            db, tenant_id=tenant, name="odoo/password", value="hunter2", kind="plugin_credential"
        )
        env = await resolve_mcp_env(
            db,
            tenant_id=tenant,
            cfg={
                "env": {"ODOO_URL": "https://odoo"},
                "secret_env": {"ODOO_PASSWORD": "odoo/password"},
            },
            connection_name="odoo",
        )
    assert env == {"ODOO_URL": "https://odoo", "ODOO_PASSWORD": "hunter2"}


async def test_an_oauth_ref_is_minted_fresh_from_the_named_connection(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = await _oauth_connection(db, tenant, "app-client-id")
        # Whatever was minted while proving the credential is now stale as far
        # as this test is concerned: expiring it forces a real re-mint here.
        conn.expires_at = None
        await db.flush()

        env = await resolve_mcp_env(
            db,
            tenant_id=tenant,
            cfg={
                "oauth_connection_id": str(conn.id),
                "secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"},
            },
            connection_name="microsoft365",
        )
    assert env == {"GRAPH_ACCESS_TOKEN": "fresh-token"}


async def test_an_oauth_ref_without_a_connection_says_so_instead_of_launching(
    app_session: AppSessionFactory,
) -> None:
    """The failure mode this replaces was a bridge started with no token at all,
    which fails later as an unexplained 401 from Graph."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(RuntimeError) as caught:
            await resolve_mcp_env(
                db,
                tenant_id=tenant,
                cfg={"secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"}},
                connection_name="microsoft365",
            )
    message = str(caught.value)
    assert "oauth_connection_id" in message
    assert "setup form" in message


class _FinalAnswerRouter:
    async def complete(self, req: Any) -> CompletionResult:
        return CompletionResult(
            text="nothing to do",
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


class _RecordingSession:
    """Stands in for the real bridge subprocess: records the environment it
    would have been launched with, offers no tools."""

    env: dict[str, str] = {}

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        *,
        timeout_s: float | None = None,
        on_step: Any = None,
    ) -> None:
        _RecordingSession.env = dict(env or {})
        self.tools: list[Any] = []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


async def test_the_engine_launches_the_bridge_with_a_freshly_minted_token(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine is the caller that matters: resolving correctly in a helper
    nobody launches from would leave the bridge exactly as broken as before."""
    tenant = uuid.uuid4()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _FinalAnswerRouter())
    # engine.py no longer constructs McpSession itself -- it goes through
    # open_tool_session (agent/mcp_client.py), which is the module that
    # actually binds the name `McpSession` used to build a stdio session.
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _RecordingSession)

    async with app_session(tenant) as db:
        oauth_conn = await _oauth_connection(db, tenant, "engine-client-id")
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora")
        db.add(agent)
        await db.flush()
        mcp_conn = m.McpConnection(
            tenant_id=tenant,
            department_id=dept.id,
            name="microsoft365",
            transport="stdio",
            server_url="",
            scopes={"read": [], "send": []},
            config={
                "command": "python",
                "args": ["-m", "mcp_bridge"],
                "env": {"PYTHONPATH": "/app/capas/microsoft365"},
                "secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"},
                "oauth_connection_id": str(oauth_conn.id),
            },
            connected=True,
            health={},
        )
        db.add(mcp_conn)
        await db.flush()

        result = await run_agent(
            db,
            agent=agent,
            task_text="say hello",
            tenant_id=tenant,
            mcp_conn=mcp_conn,
        )

    assert result.status == "done"
    assert _RecordingSession.env["GRAPH_ACCESS_TOKEN"] == "fresh-token"
    # The manifest's own environment reaches the bridge untouched alongside it.
    assert _RecordingSession.env["PYTHONPATH"] == "/app/capas/microsoft365"


#: A real FastMCP server bound to a real port, reused verbatim from
#: test_mcp_client_http_transport.py's fixture -- this codebase duplicates
#: small fixture helpers per file rather than sharing a conftest for them
#: (see test_mcp_client_tool_error.py's own `_server()`).
_ECHO_SERVER = """
import asyncio
import sys

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    # Fallback for older mcp versions
    from mcp.server.fastmcp import FastMCP as MCPServer

port = int(sys.argv[1])
mcp = MCPServer("echo")


@mcp.tool()
def echo(text: str) -> str:
    "Returns its input unchanged."
    return text


async def main():
    await mcp.run_streamable_http_async(host="127.0.0.1", port=port, streamable_http_path="/mcp")


if __name__ == "__main__":
    asyncio.run(main())
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return
        except Exception as exc:  # server not accepting connections yet
            last_exc = exc
            time.sleep(0.1)
    raise TimeoutError(f"server at {url} never came up") from last_exc


@pytest.fixture
def echo_http_server(tmp_path: Path):
    script = tmp_path / "echo_server.py"
    script.write_text(_ECHO_SERVER)
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(script), str(port)])
    try:
        _wait_until_up(f"http://127.0.0.1:{port}/mcp")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


class _CallsEchoThenStops:
    """Records which tools the loop offered the model, then drives one real
    tool call so a genuine remote MCP round trip happens inside the run --
    not merely tool discovery."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_tool_names: list[str] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        self.seen_tool_names = [t.name for t in req.tools]
        if self.calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})],
                usage=Usage(1, 1),
                stop_reason="tool_use",
                provider="fake",
                model="fake",
            )
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(1, 1),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def test_the_engine_reaches_a_real_remote_mcp_server_over_http(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    echo_http_server: str,
) -> None:
    """A `transport="http"` connection must route the in-process agent loop
    through `open_tool_session`'s remote branch -- not just at "test
    connection" time (api/v1/mcp.py's `test_connection`, Task 5) -- so the
    model sees the real remote tool and the loop can actually call it."""
    tenant = uuid.uuid4()
    router = _CallsEchoThenStops()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)

    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="Ops",
            frame={"tools": {"echo": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora")
        db.add(agent)
        await db.flush()
        mcp_conn = m.McpConnection(
            tenant_id=tenant,
            department_id=dept.id,
            name="echo",
            transport="http",
            server_url=echo_http_server,
            scopes={"read": [], "send": []},
            config={},
            connected=True,
            health={},
        )
        db.add(mcp_conn)
        await db.flush()

        result = await run_agent(
            db,
            agent=agent,
            task_text="say hi",
            tenant_id=tenant,
            mcp_conn=mcp_conn,
        )

    assert result.status == "done"
    assert "echo" in router.seen_tool_names
    assert result.tool_calls[0]["tool"] == "echo"
    assert result.tool_calls[0]["result"] == "hi"


async def test_a_delegated_ref_mints_a_token_for_the_named_mailbox(
    app_session, monkeypatch
) -> None:
    from oc8.agent import mcp_env

    calls: list[dict[str, object]] = []

    async def fake_mint_delegated_token(db, *, tenant_id, connection_id, subject, scope):
        calls.append(
            {
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "subject": subject,
                "scope": scope,
            }
        )
        return f"delegated-token-for-{subject}"

    monkeypatch.setattr(mcp_env, "mint_delegated_token", fake_mint_delegated_token)
    tenant = uuid.uuid4()
    connection_id = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await mcp_env.resolve_mcp_env(
            db,
            tenant_id=tenant,
            cfg={
                "oauth_connection_id": str(connection_id),
                "delegated_scope": "gmail.modify calendar",
                "secret_env": {"GOOGLE_DELEGATED_TOKEN_0": "oauth-delegated:agents@company.com"},
            },
            connection_name="google_workspace",
        )
    assert env["GOOGLE_DELEGATED_TOKEN_0"] == "delegated-token-for-agents@company.com"
    assert calls[0]["subject"] == "agents@company.com"
    assert calls[0]["scope"] == "gmail.modify calendar"


async def test_a_delegated_ref_with_no_oauth_connection_id_says_so(app_session) -> None:
    from oc8.agent import mcp_env

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(RuntimeError, match="oauth_connection_id"):
            await mcp_env.resolve_mcp_env(
                db,
                tenant_id=tenant,
                cfg={
                    "secret_env": {"GOOGLE_DELEGATED_TOKEN_0": "oauth-delegated:agents@company.com"}
                },
                connection_name="google_workspace",
            )


def test_has_oauth_ref_treats_a_delegated_ref_as_expiring() -> None:
    from oc8.agent.mcp_env import has_oauth_ref

    assert has_oauth_ref({"secret_env": {"X": "oauth-delegated:a@b.com"}}) is True


async def test_resolving_the_same_delegated_ref_twice_does_not_re_mint(
    app_session: AppSessionFactory,
) -> None:
    """mcp_env keeps NO cache of its own -- it calls `mint_delegated_token` on
    every resolution and relies entirely on that function's own
    `(connection_id, subject)`-keyed, expiry-aware cache (Task 1) to avoid
    redundant work. Simulate two separate bridge launches resolving the same
    `oauth-delegated:` ref in quick succession: only ONE real token mint
    (HTTP round trip) should happen."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from oc8.agent import mcp_env
    from oc8.oauth.tokens import access_ref, refresh_ref

    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={"access_token": "delegated-once", "expires_in": 3600})

    oauth_http.set_transport_override(httpx.MockTransport(handler))
    tenant = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            conn = m.OAuthConnection(
                tenant_id=tenant,
                provider="google",
                account_label="svc@my-project.iam.gserviceaccount.com",
                access_secret_ref=access_ref(uuid.uuid4()),
                client_source="tenant",
                grant_type="service_account",
                status="active",
            )
            db.add(conn)
            await db.flush()
            conn.access_secret_ref = access_ref(conn.id)
            conn.refresh_secret_ref = refresh_ref(conn.id)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private_key_pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode()
            await store_secret(
                db,
                tenant_id=tenant,
                name=conn.refresh_secret_ref,
                value=private_key_pem,
                kind="oauth_token",
            )
            await db.flush()

            cfg = {
                "oauth_connection_id": str(conn.id),
                "delegated_scope": "gmail.modify",
                "secret_env": {"GOOGLE_DELEGATED_TOKEN_0": "oauth-delegated:agents@company.com"},
            }
            env1 = await mcp_env.resolve_mcp_env(
                db, tenant_id=tenant, cfg=cfg, connection_name="google_workspace"
            )
            env2 = await mcp_env.resolve_mcp_env(
                db, tenant_id=tenant, cfg=cfg, connection_name="google_workspace"
            )
    finally:
        oauth_http.set_transport_override(None)

    assert env1 == env2 == {"GOOGLE_DELEGATED_TOKEN_0": "delegated-once"}
    assert http_calls == 1


async def test_one_unmintable_delegated_identity_does_not_take_the_whole_launch_down(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-branch review C1. A connection carrying any oauth-* ref is
    `reusable=False`, so a FRESH bridge is launched per tool call and this
    function re-mints for EVERY configured delegated identity on every call --
    including a `drive_list` that touches no mailbox at all. Letting one failed
    mint propagate meant the day a single mailbox was offboarded, suspended or
    misspelled (Google rejects that `sub` claim at mint time), the bridge never
    launched and ALL of the pack's tools went dark, including every tool that
    never uses delegation.

    So: the failure is contained to the one identity. Its env var is launched
    EMPTY, and the bridge's own `resolve_mailbox_token` raises a precise "no
    token available for mailbox X" at the one tool call that actually needed
    X. The self `oauth:` token is deliberately NOT treated this way -- it is
    what every non-delegated tool authenticates as, so there is nothing left to
    degrade to."""
    from oc8.agent import mcp_env

    async def flaky_mint(db, *, tenant_id, connection_id, subject, scope):
        if subject == "gone@company.com":
            raise RuntimeError("provider error 400: 'unauthorized_client'")
        return f"delegated-token-for-{subject}"

    monkeypatch.setattr(mcp_env, "mint_delegated_token", flaky_mint)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        oauth_conn = await _oauth_connection(db, tenant, "partial-mint-client-id")
        env = await mcp_env.resolve_mcp_env(
            db,
            tenant_id=tenant,
            cfg={
                "oauth_connection_id": str(oauth_conn.id),
                "delegated_scope": "gmail.modify calendar",
                "env": {"GOOGLE_DELEGATED_MAILBOXES": "ok@company.com,gone@company.com"},
                "secret_env": {
                    "GOOGLE_TOKEN": "oauth:google_token",
                    "GOOGLE_DELEGATED_TOKEN_0": "oauth-delegated:ok@company.com",
                    "GOOGLE_DELEGATED_TOKEN_1": "oauth-delegated:gone@company.com",
                },
            },
            connection_name="google_workspace",
        )

    # The bridge launches at all -- that is the whole finding.
    assert env["GOOGLE_TOKEN"] == "fresh-token"
    assert env["GOOGLE_DELEGATED_TOKEN_0"] == "delegated-token-for-ok@company.com"
    # ...and the broken identity is present-but-empty, not missing and not fatal.
    assert env["GOOGLE_DELEGATED_TOKEN_1"] == ""

    # What the bridge then does with that environment: mailbox 0 works, and only
    # a call naming the broken mailbox fails, with an error naming the MAILBOX.
    plugin_root = str(_GOOGLE_PLUGIN_ROOT)
    _evict_generic_plugin_packages()
    sys.path.insert(0, plugin_root)
    try:
        from mcp_bridge import google_api

        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert google_api.resolve_mailbox_token("ok@company.com") == (
            "delegated-token-for-ok@company.com"
        )
        with pytest.raises(RuntimeError, match="no token available for mailbox"):
            google_api.resolve_mailbox_token("gone@company.com")
    finally:
        sys.path.remove(plugin_root)
        _evict_generic_plugin_packages()


async def test_a_failing_self_oauth_mint_still_stops_the_launch(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of C1's fix: tolerating a failed DELEGATED mint must not
    quietly become "tolerate any failed mint". Every Drive/Docs/Sheets/Slides
    tool authenticates as the self identity, so a bridge launched without it
    has nothing left that works -- and a bridge that launches anyway turns one
    clear configuration error into 16 identical opaque 401s."""
    from oc8.agent import mcp_env

    async def dead_mint(db, *, tenant_id, connection_id):
        raise RuntimeError("provider error 400: 'invalid_grant'")

    monkeypatch.setattr(mcp_env, "get_access_token", dead_mint)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        oauth_conn = await _oauth_connection(db, tenant, "dead-self-client-id")
        with pytest.raises(RuntimeError, match="invalid_grant"):
            await mcp_env.resolve_mcp_env(
                db,
                tenant_id=tenant,
                cfg={
                    "oauth_connection_id": str(oauth_conn.id),
                    "secret_env": {"GOOGLE_TOKEN": "oauth:google_token"},
                },
                connection_name="google_workspace",
            )
