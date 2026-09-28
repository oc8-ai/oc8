"""mcp_pool must be able to open and cache an HTTP-transport session, not
only a stdio one -- proven against a real fixture server."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from oc8.agent import mcp_pool

pytestmark = pytest.mark.asyncio

# Same real FastMCP-over-streamable-HTTP fixture pattern as
# tests/agents/test_mcp_client_http_transport.py -- duplicated here per this
# codebase's existing per-file fixture convention.

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


async def test_pool_opens_and_calls_a_real_http_connection(echo_http_server: str) -> None:
    connection_id = uuid.uuid4()
    result = await mcp_pool.call(
        connection_id,
        command="",
        args=[],
        env={},
        tool="echo",
        arguments={"text": "pooled"},
        transport="http",
        server_url=echo_http_server,
        reusable=False,
    )
    assert result == "pooled"
