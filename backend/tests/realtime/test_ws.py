from __future__ import annotations

import uuid

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from oc8.auth.provider import get_identity_provider
from oc8.main import create_app
from oc8.realtime.bus import EventBus

# NOTE: the `redis_url` fixture (amended in Task 1) points get_settings().redis_url
# at this Redis testcontainer, so create_app's lifespan ConnectionManager and the
# test's EventBus share one Redis. Without that, nothing would be delivered.


def _token(tenant_id: uuid.UUID) -> str:
    # `org_admin` and not the `"admin"` this said for a year: `admin` is not a
    # role this deployment defines, so `permissions_for` returns the empty set for
    # it. That was invisible while the socket asked for nothing but a valid token
    # and is not any more -- the feed is gated on `run:view`, the same permission
    # `GET /activity` declares for the same rows.
    return get_identity_provider().mint(subject="op@test", tenant_id=tenant_id, role="org_admin")


@pytest.mark.asyncio
async def test_ws_delivers_only_own_tenant_events(redis_url: str) -> None:
    app = create_app()
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    bus = EventBus(redis_url)
    # TestClient runs the app (with lifespan) in a background thread.
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant_a)}") as ws_a:
            assert ws_a.receive_json()["type"] == "realtime.connected"
            with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant_b)}") as ws_b:
                assert ws_b.receive_json()["type"] == "realtime.connected"
                # publish an event for tenant A only
                await bus.publish_event(
                    tenant_a,
                    "agent.status",
                    {"agent_id": "a1", "status": "running"},
                    source="oc8/agent/a1",
                )
                got = ws_a.receive_json()
                assert got["type"] == "agent.status"
                assert got["tenantid"] == str(tenant_a)
                # tenant B must NOT receive A's event: publish a B event and prove
                # B's next frame is the B event, never A's.
                await bus.publish_event(
                    tenant_b,
                    "agent.status",
                    {"agent_id": "b1", "status": "idle"},
                    source="oc8/agent/b1",
                )
                got_b = ws_b.receive_json()
                assert got_b["tenantid"] == str(tenant_b)
                assert got_b["data"]["agent_id"] == "b1"
    await bus.close()


@pytest.mark.asyncio
async def test_ws_delivers_run_output_delta(redis_url: str) -> None:
    """The Part B plumbing check: `publish_run_output_delta` (called from
    each containerized runtime's poll loop -- opencode_runtime/codex_runtime/
    claude_code_runtime's runtime.py) rides the exact same EventBus -> Redis
    pubsub -> ConnectionManager -> WS pipeline `run.status` already proves
    above, with no runtime container involved."""
    from oc8.realtime.emit import publish_run_output_delta

    app = create_app()
    tenant = uuid.uuid4()
    run_id = uuid.uuid4()
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant)}") as ws:
            assert ws.receive_json()["type"] == "realtime.connected"
            await publish_run_output_delta(tenant, run_id=run_id, chunk="hello from the sandbox")
            got = ws.receive_json()
            assert got["type"] == "run.output_delta"
            assert got["tenantid"] == str(tenant)
            assert got["data"] == {"run_id": str(run_id), "chunk": "hello from the sandbox"}


@pytest.mark.asyncio
async def test_ws_delivers_run_tool_call(redis_url: str) -> None:
    """Same plumbing check as test_ws_delivers_run_output_delta, for the
    in-process engine's per-tool-call live event (publish_run_tool_call,
    called from agent/engine.py's step loop right after the same entry is
    persisted via oc8.runtime.run_context.append_tool_call -- see that
    module's docstring for why an already-open Live Log tab still needs
    this event even though the data is already durable)."""
    from oc8.realtime.emit import publish_run_tool_call

    app = create_app()
    tenant = uuid.uuid4()
    run_id = uuid.uuid4()
    call = {"tool": "get_record", "arguments": {"model": "res.partner"}, "result": "ok"}
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant)}") as ws:
            assert ws.receive_json()["type"] == "realtime.connected"
            await publish_run_tool_call(tenant, run_id=run_id, call=call)
            got = ws.receive_json()
            assert got["type"] == "run.tool_call"
            assert got["tenantid"] == str(tenant)
            assert got["data"] == {"run_id": str(run_id), "call": call}


@pytest.mark.asyncio
async def test_ws_delivers_run_step_timing(redis_url: str) -> None:
    """Same plumbing check as test_ws_delivers_run_tool_call, for the run step
    timeline plan's own live event (publish_run_step_timing, called from both
    runtimes right alongside their existing step-timing capture -- see
    oc8.realtime.emit's own docstring for why this carries no DB write of its
    own, unlike run.tool_call above)."""
    from oc8.realtime.emit import publish_run_step_timing

    app = create_app()
    tenant = uuid.uuid4()
    run_id = uuid.uuid4()
    timing = {
        "step": 1,
        "model_wait_ms": 120,
        "ttft_ms": 40,
        "tool_wait_ms": 5,
        "step_wall_ms": 200,
    }
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant)}") as ws:
            assert ws.receive_json()["type"] == "realtime.connected"
            await publish_run_step_timing(tenant, run_id=run_id, timing=timing)
            got = ws.receive_json()
            assert got["type"] == "run.step_timing"
            assert got["tenantid"] == str(tenant)
            assert got["data"] == {"run_id": str(run_id), "timing": timing}


@pytest.mark.asyncio
async def test_ws_delivers_run_token_delta(redis_url: str) -> None:
    """Same plumbing check as test_ws_delivers_run_output_delta and
    test_ws_delivers_run_tool_call, for Stage 2's model-token live event
    (publish_run_token_delta, called from both engines' streaming
    accumulator callback as each text fragment of a model turn arrives)."""
    from oc8.realtime.emit import publish_run_token_delta

    app = create_app()
    tenant = uuid.uuid4()
    run_id = uuid.uuid4()
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/events/ws?token={_token(tenant)}") as ws:
            assert ws.receive_json()["type"] == "realtime.connected"
            await publish_run_token_delta(tenant, run_id=run_id, text="Hal")
            got = ws.receive_json()
            assert got["type"] == "run.token_delta"
            assert got["tenantid"] == str(tenant)
            assert got["data"] == {"run_id": str(run_id), "text": "Hal"}


def test_ws_rejects_missing_token(redis_url: str) -> None:
    app = create_app()
    with TestClient(app) as client:
        # starlette closes the socket (1008) on reject -> WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/events/ws"):
                pass


def test_ws_rejects_bad_token(redis_url: str) -> None:
    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/events/ws?token=not-a-jwt"):
                pass


def test_ws_rejects_an_agent_token(redis_url: str) -> None:
    """This feed is the operator's view of a tenant: every run, approval and
    activity in it. An agent token is minted for a container, so subscribing with
    one would let the container watch the whole tenant -- including approvals it
    is itself waiting on. The operator API refuses agent tokens through a router
    dependency; a WebSocket cannot carry one, so it is refused here instead."""
    import uuid as _uuid

    from oc8.auth import get_identity_provider

    token = get_identity_provider().mint(
        tenant_id=_uuid.uuid4(),
        subject=f"agent:{_uuid.uuid4()}",
        role="agent_default",
        kind="agent",
        scopes=[f"run:{_uuid.uuid4()}"],
    )
    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/v1/events/ws?token={token}"):
                pass


@pytest.mark.parametrize("totp_scope", ["totp:enroll", "totp:challenge"])
def test_ws_rejects_a_totp_pending_token(redis_url: str, totp_scope: str) -> None:
    """A `totp:enroll`/`totp:challenge`-scoped token proves a password at most
    -- the second factor is still outstanding -- so it must not be able to
    open this feed and stream the tenant's entire live event stream while
    that factor remains unproven. The operator API refuses these tokens via
    `deny_totp_pending_principals`, a router dependency; a WebSocket cannot
    carry one, so `ws.py` refuses it in place instead (same shape as
    `test_ws_rejects_an_agent_token` above)."""
    token = get_identity_provider().mint(
        tenant_id=uuid.uuid4(),
        subject="pending@example.com",
        role="org_admin",
        scopes=[totp_scope],
    )
    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/v1/events/ws?token={token}"):
                pass

