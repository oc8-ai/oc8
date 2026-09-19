"""Keep a bridge's session open instead of rebuilding it for every tool call.

Measured on the live system, taking one session apart:

    process spawn   0.011s
    initialize()    2.976s     <- all of it
    list_tools()    0.021s
    call on an open session   0.626s, then 0.062s, 0.049s

So the cost is not starting the bridge -- that is eleven milliseconds -- it is
the MCP handshake, which for a bridge like Odoo's includes authenticating
against the far system. Paying it per call turned a 50ms query into ~1.7s, and
a run making ten calls into seventeen seconds of waiting for nothing.

**The smallest thing that holds** (§1.4). The obvious answer is a pool with a
lifetime, an eviction loop, a size cap and health checks. Almost none of that
earns its keep here, because the cache is keyed by CONNECTION and a deployment
has a handful of those -- one or two per department, set up by an operator. The
number of live sessions is therefore bounded by configuration, not by traffic,
which is what makes the missing pieces safe to leave out:

* no eviction loop -- an idle session costs a subprocess, and there can only
  ever be as many as there are connections;
* no size cap -- same reason;
* no health probe -- a broken session announces itself by raising on the next
  call, and that is when it is rebuilt.

What it does need, and why:

* **a lock per session** -- two tool calls for one connection can arrive at
  once, and one stdio pipe cannot serve both halfway through;
* **rebuild on failure** -- a bridge that died leaves a cached session that
  fails every call after it, forever, which is worse than no cache at all;
* **a maximum age** -- checked when the session is next used rather than by a
  timer, so a rotated credential or a changed config eventually takes effect
  without anything having to watch for it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from oc8.agent.mcp_client import HttpToolSession, McpSession

logger = logging.getLogger(__name__)

#: How long a session may be reused before it is rebuilt. Not a health measure
#: -- a dead session is caught by its next call raising. This is what lets a
#: rotated secret or an edited connection take effect without a restart, and it
#: bounds how stale the far system's view of us can get.
MAX_AGE = dt.timedelta(minutes=30)


class McpSessionFailed(RuntimeError):
    """A session could not be opened. A plain exception on purpose: the failure
    it replaces was a `CancelledError`, which is not an `Exception` and so
    escaped every handler between the pool and the request."""


@dataclass
class _Live:
    session: McpSession | HttpToolSession
    opened_at: dt.datetime
    #: The task that entered the session and is the only one that may leave it.
    owner: asyncio.Task[None]
    #: Set to ask the owner to close.
    stop: asyncio.Event
    #: One stdio pipe cannot serve two calls at once.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def _own(
    command: str,
    args: list[str],
    env: dict[str, str],
    ready: asyncio.Future[McpSession | HttpToolSession],
    stop: asyncio.Event,
    *,
    transport: str = "stdio",
    server_url: str = "",
    headers: dict[str, str] | None = None,
    http_tools: list[dict[str, Any]] | None = None,
) -> None:
    """Hold one session open for its whole life, in ONE task.

    The reason this task exists is a bug that cost an agent everything it had.
    The session used to be entered with a bare `await session.__aenter__()` in
    whichever request happened to open it, and left later from a different one.
    That works right up until the far system fails: the MCP stdio client is
    built on an anyio task group, and its cancel scope belongs to the task that
    entered it. A failure cancels that scope -- which means it cancels the
    REQUEST that opened it, with a `CancelledError` that is not an `Exception`
    and therefore slips through every `except Exception` on the way out.

    Observed live 2026-07-30: an Odoo endpoint answered 404, and the agent lost
    not only Odoo but its memory, its knowledge and every skill -- the whole
    tools/list 500'd -- then reported "no tool is available to me", which reads
    like a misconfiguration and was not one.

    So the session is entered and left here, in a task of its own. When the far
    system fails, THIS task is cancelled and the caller sees a plain exception.
    """
    try:
        session_obj: McpSession | HttpToolSession
        if transport == "manual_http":
            session_obj = HttpToolSession(server_url, http_tools or [], headers=headers)
        elif transport == "http":
            session_obj = McpSession(
                "", [], env, transport="http", server_url=server_url, headers=headers
            )
        else:
            session_obj = McpSession(command, args, env=env)
        async with session_obj as session:
            ready.set_result(session)
            await stop.wait()
    except BaseException as exc:  # reported to the caller through `ready`
        if not ready.done():
            ready.set_exception(exc)


async def _start(
    connection_id: uuid.UUID,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    stamp: dt.datetime,
    transport: str = "stdio",
    server_url: str = "",
    headers: dict[str, str] | None = None,
    http_tools: list[dict[str, Any]] | None = None,
    keep: bool = True,
) -> _Live:
    """Open a session and return it, or raise what the far system raised.

    `keep=False` leaves it out of the cache -- it is still owned by its own
    task (that is what `_own` is for and has nothing to do with pooling), the
    caller simply closes it when done.
    """
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[McpSession | HttpToolSession] = loop.create_future()
    stop = asyncio.Event()
    owner = asyncio.create_task(
        _own(
            command,
            args,
            env,
            ready,
            stop,
            transport=transport,
            server_url=server_url,
            headers=headers,
            http_tools=http_tools,
        )
    )
    try:
        session = await ready
    except asyncio.CancelledError:
        stop.set()
        owner.cancel()
        # Whose cancellation was it? If the owner is already finished, the far
        # system took itself down and the caller must see a normal failure it can
        # handle -- a CancelledError would sail through every `except Exception`
        # between here and the request handler, which is the bug this whole file
        # was rewritten for. If the owner is still alive, WE are the ones being
        # cancelled and that must be honoured.
        if not owner.done():
            raise
        raise McpSessionFailed(f"connection {connection_id} closed while starting") from None
    except BaseException:
        stop.set()
        owner.cancel()
        raise
    entry = _Live(session=session, opened_at=stamp, owner=owner, stop=stop)
    if keep:
        _LIVE[connection_id] = entry
    return entry


#: Per process. The gateway, the isolated step API and the in-process engine each
#: keep their own, which is correct: a session belongs to the process holding the
#: pipe, and sharing one across processes would need a broker nobody needs.
_LIVE: dict[uuid.UUID, _Live] = {}


async def _close(entry: _Live, connection_id: uuid.UUID, why: str) -> None:
    """Ask the owner to let go, and wait for it. Never leaves the owner running:
    a session nobody closes is a subprocess nobody reaps."""
    entry.stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(entry.owner), timeout=10)
    except Exception:  # a bridge that will not close cleanly is still gone
        entry.owner.cancel()
        logger.debug("closing session for %s (%s) raised", connection_id, why, exc_info=True)


@asynccontextmanager
async def _single_use(
    connection_id: uuid.UUID,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    stamp: dt.datetime,
    transport: str = "stdio",
    server_url: str = "",
    headers: dict[str, str] | None = None,
    http_tools: list[dict[str, Any]] | None = None,
) -> AsyncIterator[McpSession | HttpToolSession]:
    """A session for exactly one use, then closed. Any cached one is dropped
    first: it was started with an environment we have now decided not to trust.
    """
    stale = _LIVE.pop(connection_id, None)
    if stale is not None:
        await _close(stale, connection_id, "environment expires")
    entry = await _start(
        connection_id,
        command=command,
        args=args,
        env=env,
        stamp=stamp,
        transport=transport,
        server_url=server_url,
        headers=headers,
        http_tools=http_tools,
        keep=False,
    )
    try:
        yield entry.session
    finally:
        await _close(entry, connection_id, "single use")


async def call(
    connection_id: uuid.UUID,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    tool: str,
    arguments: dict[str, Any],
    transport: str = "stdio",
    server_url: str = "",
    headers: dict[str, str] | None = None,
    http_tools: list[dict[str, Any]] | None = None,
    now: dt.datetime | None = None,
    reusable: bool = True,
) -> str:
    """Run one tool call, reusing this connection's session where possible.

    Raises whatever the bridge raises, having first dropped the session so the
    next caller starts a fresh one. Callers already turn an exception here into
    a readable tool error; hiding it would only lose the reason.

    `reusable=False` opts a connection out of the cache entirely, and is what
    an expiring environment needs: `env` is applied when the session STARTS and
    never revisited, so a connection holding a minted OAuth token would keep
    presenting an expired one for the rest of `MAX_AGE`. Nothing here could
    notice, either -- a bridge that answers `401` returns an error *result*,
    which is not an exception, so the poisoned-session eviction below never
    fires. The handshake is paid per call for these; correctness is worth more
    than the ~3s it saves, and a token-bearing bridge does no far-system
    authentication during startup anyway (that cost was Odoo's).
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)

    if not reusable:
        async with _single_use(
            connection_id,
            command=command,
            args=args,
            env=env,
            stamp=stamp,
            transport=transport,
            server_url=server_url,
            headers=headers,
            http_tools=http_tools,
        ) as session:
            return await session.call(tool, arguments)

    entry = _LIVE.get(connection_id)

    if entry is not None and stamp - entry.opened_at > MAX_AGE:
        _LIVE.pop(connection_id, None)
        await _close(entry, connection_id, "aged out")
        entry = None

    if entry is None:
        entry = await _start(
            connection_id,
            command=command,
            args=args,
            env=env,
            stamp=stamp,
            transport=transport,
            server_url=server_url,
            headers=headers,
            http_tools=http_tools,
        )

    async with entry.lock:
        try:
            return await entry.session.call(tool, arguments)
        except Exception:
            # Poisoned: every later call on this session would fail the same
            # way. Dropping it costs one handshake; keeping it costs the
            # connection until the process restarts.
            if _LIVE.get(connection_id) is entry:
                _LIVE.pop(connection_id, None)
            await _close(entry, connection_id, "call failed")
            raise


async def tools(
    connection_id: uuid.UUID,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    transport: str = "stdio",
    server_url: str = "",
    headers: dict[str, str] | None = None,
    http_tools: list[dict[str, Any]] | None = None,
    now: dt.datetime | None = None,
    reusable: bool = True,
) -> list[Any]:
    """The tools this connection offers, from the same reused session.

    `reusable=False` as in `call` above: an expiring environment must not be
    left holding a session open behind it.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    if not reusable:
        async with _single_use(
            connection_id,
            command=command,
            args=args,
            env=env,
            stamp=stamp,
            transport=transport,
            server_url=server_url,
            headers=headers,
            http_tools=http_tools,
        ) as session:
            return list(session.tools)

    entry = _LIVE.get(connection_id)
    if entry is not None and stamp - entry.opened_at > MAX_AGE:
        _LIVE.pop(connection_id, None)
        await _close(entry, connection_id, "aged out")
        entry = None
    if entry is None:
        entry = await _start(
            connection_id,
            command=command,
            args=args,
            env=env,
            stamp=stamp,
            transport=transport,
            server_url=server_url,
            headers=headers,
            http_tools=http_tools,
        )
    return list(entry.session.tools)


async def close_all() -> None:
    """Give up every session. For shutdown and for tests."""
    for connection_id, entry in list(_LIVE.items()):
        _LIVE.pop(connection_id, None)
        await _close(entry, connection_id, "closing")
