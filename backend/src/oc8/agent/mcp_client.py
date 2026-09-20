"""MCP tool client — opens a stdio session to an MCP server for the duration of
an agent run, exposes its tools as neutral schemas, and invokes them."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import tempfile
import urllib.parse
from contextlib import AsyncExitStack
from importlib.metadata import version as _pkg_version
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

import httpx
import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from oc8.constants import CORE_VERSION
from oc8.modelrouter.types import NeutralTool

if TYPE_CHECKING:
    from typing import TextIO

#: How long one request to a tool server may take before it is a failed tool
#: call rather than a hung run. THE FAILURE: the SDK's default is None, i.e.
#: wait for ever (verified against the installed mcp 1.28.1 --
#: `ClientSession(read, write)` and `call_tool(name, args)` both default
#: `read_timeout_seconds` to None). A tool server that stops answering therefore
#: hung the whole in-process agent loop, and after 2026-08-02 that is no longer
#: survivable: the worker renews its queue claim and its database heartbeat for
#: as long as its handler runs, and both of those reports are HONEST here -- the
#: task really is alive, it is simply never going to finish. So neither decider
#: fires, the run stays `running` for ever, its agent stays busy for ever, and
#: because the worker awaits its handler inline, that worker takes no further
#: work either. The five-minute reclaim used to close the row (never the wedge);
#: this closes the wedge, which is the half that was never covered.
#:
#: Generous on purpose -- a real tool call reaches a CRM over the network, and
#: the point is to bound a hang, not to hurry work. It matches the model call's
#: own allowance (modelrouter.streaming, httpx timeout=180) so one step of the
#: loop has one order of magnitude, and `agent_max_steps` then bounds the run.
#: The engine already turns a raising tool call into "ERROR: ..." for the model,
#: so a timeout costs one step and the agent can say what it could not reach.
MCP_REQUEST_TIMEOUT_SECONDS = 180.0

logger = logging.getLogger(__name__)

#: Which major of the SDK is actually installed here -- see `_read_timeout`.
try:
    _MCP_MAJOR = int(_pkg_version("mcp").split(".")[0])
except Exception:  # pragma: no cover - a missing dist would break the import anyway
    _MCP_MAJOR = 1

# Only these host vars are forwarded to an MCP server subprocess. The full host
# environment is deliberately NOT passed so provider keys and DB credentials never
# leak into a (possibly third-party) tool server.
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT")

#: Ships with oc8 core -- its sitecustomize.py gives every MCP subprocess a
#: real `oc8/<version>` User-Agent for outbound HTTP (odoo_mcp's raw urllib
#: calls today, any future tool pack's), instead of Python's bare
#: `Python-urllib/<pyver>` default. A server sitting behind a WAF (Cloudflare,
#: etc.) can then allowlist oc8 by name rather than by IP, or block the
#: generic default outright. Python's `site` module imports `sitecustomize`
#: automatically for every interpreter start as long as this directory is on
#: PYTHONPATH -- true even for a `uv tool run <pkg>` console-script
#: entrypoint, confirmed live against the real odoo_mcp subprocess
#: (2026-09-02). This is core, not plugin, code: it patches Python's own
#: urllib default, not any one vendor's behaviour, so it belongs here rather
#: than under a specific capa's setup.
_SITECUSTOMIZE_DIR = str(Path(__file__).resolve().parent / "_mcp_sitecustomize")


def _safe_env(overrides: dict[str, str] | None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    # Workspace/config for the demo servers, but never secrets.
    env.update({k: v for k, v in os.environ.items() if k.startswith("OC8_MCP_")})
    env.update(overrides or {})
    env["OC8_USER_AGENT"] = f"oc8/{CORE_VERSION}"
    # Prepended, not overwritten: a plugin (microsoft365, google_workspace)
    # may already need its own PYTHONPATH entry for its bridge package
    # (tool_pack.toml's env.PYTHONPATH) -- both must stay importable.
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        os.pathsep.join([_SITECUSTOMIZE_DIR, existing_pythonpath])
        if existing_pythonpath
        else _SITECUSTOMIZE_DIR
    )
    return env


def _read_timeout(seconds: float) -> Any:
    """The request timeout in whatever type the installed SDK adds to its clock.

    mcp 1.x stores this and calls `.total_seconds()` on it, so it needs a
    `timedelta`. mcp 2.x adds it to a monotonic reading directly, so it needs a
    float, and a `timedelta` raises `unsupported operand type(s) for +: 'float'
    and 'datetime.timedelta'` on the FIRST request -- which is `initialize`, so
    the session never opens and the agent is handed no tools at all.

    THIS IS UNTESTABLE FROM THE SUITE, and that is the point of the shim rather
    than a fix for one major: the lockfile resolves mcp 1.28.1, the container
    image resolves 2.0.0 (measured 2026-08-02), because the dependency is pinned
    open. So the tests run one major and production runs the other, and only a
    live run can tell them apart. It took one: Nora answered "ohne Zugriff auf
    die Odoo-CRM-Schnittstelle", `tool_routes` was empty, and the connection
    test reported that TypeError verbatim.

    `_schema_of` below carries the same scar from the same open pin -- the
    `inputSchema` -> `input_schema` rename in 2.0, found the same way. Two is a
    pattern: either pin the major, or keep writing these.
    """
    return seconds if _MCP_MAJOR >= 2 else dt.timedelta(seconds=seconds)


class McpServerStartupError(RuntimeError):
    """A stdio MCP server failed to come up, with the reason it printed."""


#: `package.module.SomeError: message` -- a raised exception with its origin,
#: as opposed to the bare `Error: ...` a program prints on its way out.
_QUALIFIED_EXCEPTION = re.compile(r"^[A-Za-z_][\w.]*\.[A-Z]\w*(Error|Exception|Fault):\s+\S")


class _StderrTail:
    """Captures an MCP server's stderr so its last words can be reported.

    `stdio_client` writes the child's stderr to whatever it is handed and
    defaults to ours -- so the reason a server refused to start reached the
    container log and nowhere else.

    A REAL temporary file, not an in-memory object: the SDK hands this straight
    to `subprocess` as the child's stderr, which needs a file descriptor, so a
    duck-typed sink fails with `'_StderrTail' object has no attribute 'fileno'`.
    It is unlinked at creation and closed with the session, so nothing survives
    on disk.
    """

    #: A server that dies mid-traceback must not be able to make us read a
    #: large file into memory to find one sentence.
    _TAIL_BYTES = 8192

    def __init__(self) -> None:
        self._file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")

    def fileno(self) -> int:
        return self._file.fileno()

    def tee_to_log(self, *, connection: str) -> None:
        """Copy what the server said into our own log as well.

        Capturing alone was a regression: the first version of this class took
        the child's stderr away from the container log to put one line on the
        screen, and the full traceback -- the thing you actually debug with --
        stopped existing anywhere. One summarised line for the operator, the
        whole thing for whoever has to diagnose it.
        """
        try:
            self._file.flush()
            self._file.seek(0)
            captured = self._file.read()
        except (OSError, ValueError):
            return
        if captured.strip():
            logger.warning("mcp server %s stderr:\n%s", connection, captured[-8000:])

    def write(self, text: str) -> int:
        return self._file.write(text)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def last_meaningful_line(self) -> str:
        """The most useful single line for an operator, or "".

        THE RULE: prefer the FIRST fully-qualified exception line, because
        Python prints a chained traceback cause-before-effect. Taking the last
        line instead -- which this did at first -- reported Odoo's
        `MCPSystemError: Unexpected error: unhandled errors in a TaskGroup
        (1 sub-exception)` while the line that actually helps,
        `OdooConnectionError: Authentication failed: Username/password
        authentication failed`, sat six lines above it.

        "Fully qualified" is the discriminator: a real cause arrives as
        `package.module.SomeError: message`, whereas the generic wrappers a
        server prints on its way out are bare (`Error: ...`). Falls back to the
        last non-frame line when nothing matches, which covers servers that
        just print a sentence and exit.
        """
        try:
            self._file.flush()
            self._file.seek(0, os.SEEK_END)
            size = self._file.tell()
            self._file.seek(max(0, size - self._TAIL_BYTES))
            captured = self._file.read()
        except (OSError, ValueError):  # already closed, or not seekable
            return ""

        # ExceptionGroup output prefixes its members with `|`; strip that
        # before matching, or every line inside a group looks like noise.
        lines = [line.strip().lstrip("|+-").strip() for line in captured.splitlines()]

        for line in lines:
            if _QUALIFIED_EXCEPTION.match(line):
                return line[:300]

        for line in reversed(lines):
            if not line or line.startswith(('File "', "Traceback")):
                continue
            return line[:300]
        return ""


def _schema_of(tool: Any) -> dict[str, Any]:
    """A tool's parameter schema, whichever major of the MCP SDK is installed.

    The wire field is `inputSchema` in both, but the Python attribute was renamed
    to `input_schema` in mcp 2.0 -- and an open `mcp>=1.2` pulled that in on a
    routine image rebuild. Every tool call then died with an AttributeError that
    named the SDK, not the cause, so the whole integration looked broken.
    """
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    return schema or {"type": "object", "properties": {}}


class McpSession:
    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        *,
        transport: str = "stdio",
        server_url: str = "",
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._transport = transport
        self._server_url = server_url
        self._headers = dict(headers or {})
        self._params = (
            StdioServerParameters(command=command, args=args, env=_safe_env(env))
            if transport == "stdio"
            else None
        )
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._timeout_s = MCP_REQUEST_TIMEOUT_SECONDS if timeout_s is None else timeout_s
        self.tools: list[NeutralTool] = []
        self._errlog = _StderrTail()

    async def __aenter__(self) -> McpSession:
        try:
            if self._transport == "http":
                # Create an HTTP client with configured headers and timeout,
                # then pass it to the streamable-HTTP transport.
                http_client = await self._stack.enter_async_context(
                    create_mcp_http_client(
                        headers=self._headers or None,
                        timeout=httpx2.Timeout(self._timeout_s) if self._timeout_s else None,
                    )
                )
                read, write = await self._stack.enter_async_context(
                    streamable_http_client(
                        self._server_url,
                        http_client=http_client,
                    )
                )
            else:
                read, write = await self._stack.enter_async_context(
                    # Capture the server's stderr instead of letting it default to
                    # ours. When a stdio server dies during start-up the SDK raises
                    # a transport-level error -- "Connection closed" -- which says
                    # nothing about WHY. The reason is on the child's stderr, and
                    # without this it reached the operator's screen not at all: an
                    # Odoo connection refused with a precise `403: MCP Server is
                    # disabled globally` was shown as "Connection closed", and read
                    # as a credentials problem.
                    # cast: the parameter is typed `TextIO`, but the SDK only ever
                    # writes and flushes it, which is all `_StderrTail` implements.
                    stdio_client(self._params, errlog=cast("TextIO", self._errlog))
                )
            # The session default, so it covers the handshake too: `initialize` and
            # `list_tools` are requests like any other, and a server that never
            # answers the first of them hung the run before it had done anything.
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=_read_timeout(self._timeout_s))
            )
            await self._session.initialize()
            listed = await self._session.list_tools()
        except BaseException as exc:
            # A server that never answers `initialize` raises here -- but the
            # subprocess and session are already pushed onto the stack, and
            # __aexit__ is never called when __aenter__ raises. Without this,
            # that child process leaks for as long as it keeps running.
            await self._stack.aclose()
            reason = self._errlog.last_meaningful_line() if self._transport == "stdio" else ""
            if self._transport == "stdio":
                self._errlog.tee_to_log(connection=self._params.command)  # type: ignore[union-attr]
            self._errlog.close()
            if reason and isinstance(exc, Exception):
                raise McpServerStartupError(f"{exc or type(exc).__name__}: {reason}") from exc
            raise
        self.tools = [
            NeutralTool(
                name=t.name,
                description=t.description or "",
                parameters=_schema_of(t),
            )
            for t in listed.tools
        ]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self._stack.aclose()
        finally:
            # The stderr capture outlives the subprocess by design (its last
            # words are read after the pipe closes), so it is this object's to
            # release -- and a long-lived pool holds many sessions.
            self._errlog.close()

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise RuntimeError("MCP session not started")
        result = await self._session.call_tool(name, arguments)
        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        text = "\n".join(parts) if parts else "(no output)"
        # The MCP protocol marks a tool-level failure (the server ran the
        # tool, the tool itself rejected the call -- e.g. Odoo refusing an
        # unknown field) via `is_error`, not an exception; call_tool() never
        # raises for this. Every caller of Toolset.call() already treats an
        # exception here as the tool failing (`except Exception as exc:
        # output = f"ERROR: {exc}"` in engine.py/internal_agent.py), so
        # without this check that convention silently never fired for a real
        # protocol-level tool error -- PostToolUseFailure never dispatched,
        # and the department cache (oc8.agent.cache_flow) had no signal to
        # avoid replaying the failing request. Live-observed 2026-08-26.
        if result.is_error:
            raise RuntimeError(text)
        return text


class HttpToolSession:
    """A drop-in substitute for `McpSession` over a plain REST API with no
    MCP support at all: tools are declared manually in the connection's
    config (`transport="manual_http"`) rather than discovered via a
    handshake, so `__aenter__` performs no network I/O."""

    def __init__(
        self,
        base_url: str,
        http_tools: list[dict[str, Any]],
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_tools = {str(t["name"]): t for t in http_tools}
        self._headers = dict(headers or {})
        self._timeout_s = MCP_REQUEST_TIMEOUT_SECONDS if timeout_s is None else timeout_s
        self._client: httpx.AsyncClient | None = None
        self.tools: list[NeutralTool] = []

    async def __aenter__(self) -> HttpToolSession:
        self._client = httpx.AsyncClient(timeout=self._timeout_s)
        self.tools = [
            NeutralTool(
                name=str(t["name"]),
                description=str(t.get("description", "")),
                parameters=t.get("param_schema") or {"type": "object", "properties": {}},
            )
            for t in self._http_tools.values()
        ]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._client is None:
            raise RuntimeError("HTTP tool session not started")
        tool = self._http_tools.get(name)
        if tool is None:
            raise RuntimeError(f"unknown tool {name!r}")
        url_template = str(tool.get("url_template", ""))
        placeholders = set(re.findall(r"\{(\w+)\}", url_template))
        try:
            # Quoted so a model-supplied value (e.g. containing `../`, `?` or
            # `#`) can't escape the path segment it was meant to fill or
            # rewrite the query/URL structure.
            url = self._base_url + url_template.format(
                **{k: urllib.parse.quote(str(arguments[k]), safe="") for k in placeholders}
            )
        except KeyError as exc:
            raise RuntimeError(f"missing required parameter {exc}") from exc
        # Placeholders already consumed by the URL template are not also sent
        # as a query param or body field.
        remaining = {k: v for k, v in arguments.items() if k not in placeholders}
        method = str(tool.get("method", "GET")).upper()
        try:
            if method in ("GET", "DELETE"):
                resp = await self._client.request(
                    method, url, headers=self._headers, params=remaining
                )
            else:
                resp = await self._client.request(
                    method, url, headers=self._headers, json=remaining
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"{exc.response.status_code}: {exc.response.text[:500]}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(str(exc)) from exc
        return resp.text or "(no output)"


def resolve_auth_header(cfg: dict[str, Any], env: dict[str, str]) -> dict[str, str]:
    """The single optional auth header a remote-MCP or manual-HTTP connection
    may declare (`cfg["auth_header_name"]`), resolved to its live value out
    of `env` -- the same place a stdio connection's env vars are resolved,
    since `resolve_mcp_env` (agent/mcp_env.py) already turned the declared
    `secret_env` entry into a plain value under that same header name."""
    header_name = str(cfg.get("auth_header_name", ""))
    if not header_name or header_name not in env:
        return {}
    return {header_name: env[header_name]}


async def open_tool_session(
    *,
    transport: str,
    command: str = "",
    args: list[str] | None = None,
    server_url: str = "",
    headers: dict[str, str] | None = None,
    http_tools: list[dict[str, Any]] | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> McpSession | HttpToolSession:
    """The single place that knows which session class a connection's
    transport needs -- every caller that used to construct `McpSession`
    directly calls this instead, so the branch is not repeated at each of
    them. Returns an UNENTERED session; this is a plain async function, not
    an async context manager, so callers write
    `tool_session = await open_tool_session(...)` then
    `async with tool_session as session:`."""
    if transport == "manual_http":
        return HttpToolSession(
            server_url, list(http_tools or []), headers=headers, timeout_s=timeout_s
        )
    if transport == "http":
        return McpSession(
            "", [], env, transport="http", server_url=server_url, headers=headers,
            timeout_s=timeout_s,
        )
    if transport == "stdio":
        return McpSession(command, args or [], env, timeout_s=timeout_s)
    raise ValueError(f"unsupported transport {transport!r}")
