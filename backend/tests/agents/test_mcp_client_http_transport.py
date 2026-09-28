"""McpSession must be able to reach a real remote MCP server over
streamable HTTP, not only spawn a stdio subprocess -- proven against a real
FastMCP server bound to a real port, not a mock."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from oc8.agent.mcp_client import McpSession

pytestmark = pytest.mark.asyncio

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


async def test_http_transport_discovers_and_calls_a_real_remote_tool(echo_http_server: str) -> None:
    async with McpSession("", [], transport="http", server_url=echo_http_server) as session:
        assert [t.name for t in session.tools] == ["echo"]
        result = await session.call("echo", {"text": "hello"})
        assert result == "hello"
