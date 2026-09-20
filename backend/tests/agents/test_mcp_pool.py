"""A bridge's session is reused, and dropped the moment it stops working.

Measured live: the process spawn behind a session is 11ms, `initialize()` is
2.976s, and a call on an already-open session is ~50ms. Paying the handshake per
call turned a 50ms query into 1.7s, and a ten-call run into seventeen seconds of
waiting for nothing.

The tests are mostly about the three things the cache must NOT get wrong, since
a wrong cache is worse than none.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest

from oc8.agent import mcp_pool

pytestmark = pytest.mark.asyncio


class _FakeSession:
    """Stands in for a bridge. Counts handshakes, which is the thing that costs."""

    opened = 0
    closed = 0

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        *,
        timeout_s: float | None = None,
        on_step: Any = None,
    ) -> None:
        self.command = command
        self.tools = ["a", "b"]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next = False

    async def __aenter__(self) -> _FakeSession:
        type(self).opened += 1
        return self

    async def __aexit__(self, *a: Any) -> None:
        type(self).closed += 1

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        if self.fail_next:
            raise RuntimeError("bridge went away")
        self.calls.append((tool, arguments))
        await asyncio.sleep(0.01)
        return f"ok:{tool}"


@pytest.fixture(autouse=True)
async def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeSession)
    _FakeSession.opened = 0
    _FakeSession.closed = 0
    await mcp_pool.close_all()
    yield
    await mcp_pool.close_all()


async def _call(conn: uuid.UUID, tool: str = "t", **kw: Any) -> str:
    return await mcp_pool.call(conn, command="x", args=[], env={}, tool=tool, arguments={}, **kw)


async def test_the_handshake_is_paid_once_not_once_per_call() -> None:
    conn = uuid.uuid4()
    for _ in range(5):
        assert await _call(conn) == "ok:t"
    assert _FakeSession.opened == 1, "five calls, one handshake"


async def test_each_connection_gets_its_own() -> None:
    """Two systems are two pipes. Sharing one would send Odoo's call to Gitea."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _call(a)
    await _call(b)
    assert _FakeSession.opened == 2


async def test_a_broken_session_is_dropped_rather_than_kept() -> None:
    """A cached session belonging to a bridge that died would fail every call
    after it, for ever -- worse than having no cache at all."""
    conn = uuid.uuid4()
    await _call(conn)
    live = mcp_pool._LIVE[conn]
    assert isinstance(live.session, _FakeSession)
    live.session.fail_next = True

    with pytest.raises(RuntimeError):
        await _call(conn)
    assert conn not in mcp_pool._LIVE, "the poisoned session is gone"
    assert _FakeSession.closed == 1

    assert await _call(conn) == "ok:t", "and the next call simply rebuilds"
    assert _FakeSession.opened == 2


async def test_a_session_is_rebuilt_once_it_is_old_enough() -> None:
    """Not a health check -- a dead session announces itself. This is what lets
    a rotated credential take effect without anything watching for it."""
    conn = uuid.uuid4()
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    await _call(conn, now=t0)
    await _call(conn, now=t0 + dt.timedelta(minutes=5))
    assert _FakeSession.opened == 1
    await _call(conn, now=t0 + mcp_pool.MAX_AGE + dt.timedelta(seconds=1))
    assert _FakeSession.opened == 2
    assert _FakeSession.closed == 1


async def test_calls_on_one_connection_do_not_overlap() -> None:
    """One stdio pipe cannot serve two calls halfway through each other."""
    conn = uuid.uuid4()
    await _call(conn)
    session = mcp_pool._LIVE[conn].session
    assert isinstance(session, _FakeSession)

    overlaps = 0
    inflight = 0
    original = session.call

    async def watched(tool: str, arguments: dict[str, Any]) -> str:
        nonlocal overlaps, inflight
        inflight += 1
        if inflight > 1:
            overlaps += 1
        try:
            return await original(tool, arguments)
        finally:
            inflight -= 1

    session.call = watched  # type: ignore[method-assign]
    await asyncio.gather(*(_call(conn, tool=f"t{i}") for i in range(6)))
    assert overlaps == 0
    assert _FakeSession.opened == 1


async def test_listing_tools_uses_the_same_session() -> None:
    conn = uuid.uuid4()
    assert await mcp_pool.tools(conn, command="x", args=[], env={}) == ["a", "b"]
    await _call(conn)
    assert _FakeSession.opened == 1


# ------------------------------------------------- an environment that expires


async def test_an_expiring_environment_is_never_reused() -> None:
    """`env` is applied when a session STARTS and never revisited.

    That is harmless for a password and wrong for a minted OAuth token, which
    may arrive with as little as a minute of life left: a pooled bridge would
    keep presenting an expired one for the rest of MAX_AGE, and nothing here
    could notice -- a far system answering 401 comes back as an error RESULT,
    not an exception, so the poisoned-session eviction never fires.
    """
    conn = uuid.uuid4()
    for _ in range(3):
        assert await _call(conn, reusable=False) == "ok:t"
    assert _FakeSession.opened == 3, "one handshake per call, on purpose"
    assert _FakeSession.closed == 3, "and nothing left running"
    assert conn not in mcp_pool._LIVE


async def test_a_pooled_session_is_dropped_once_its_environment_expires() -> None:
    """The same connection can change its mind -- setting the plugin up against
    an OAuth app is what does it -- and the session started under the old
    arrangement must not survive that."""
    conn = uuid.uuid4()
    await _call(conn)
    assert conn in mcp_pool._LIVE
    await _call(conn, reusable=False)
    assert conn not in mcp_pool._LIVE
    assert _FakeSession.closed == 2, "the cached one and the single-use one"


async def test_listing_tools_with_an_expiring_environment_is_not_pooled() -> None:
    conn = uuid.uuid4()
    assert await mcp_pool.tools(conn, command="x", args=[], env={}, reusable=False) == ["a", "b"]
    assert conn not in mcp_pool._LIVE
    assert _FakeSession.closed == 1
