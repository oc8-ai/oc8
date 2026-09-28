"""McpSession.call() must surface a protocol-level tool failure as an
exception, not as ordinary text.

WHY THIS EXISTS. The MCP SDK's own call_tool() never raises for a tool that
ran and then rejected the call (e.g. Odoo refusing an unknown field) -- it
comes back as an ordinary CallToolResult with `is_error=True` and the error
message in `.content`. Before this fix, mcp_client.py's call() only ever
looked at `.content`, so that result was indistinguishable from a genuine
success. Every caller (engine.py's step loop, internal_agent.py's /tool
endpoint) treats an exception from call() as the tool failing
(`except Exception as exc: output = f"ERROR: {exc}"`) -- so without this,
that convention silently never fired for a real protocol-level tool error:
PostToolUseFailure never dispatched, and the department cache had no signal
telling it the request it just cached led to a real failure. Live-observed
2026-08-26 on the odoo_mcp plugin (Odoo rejecting an invalid `sla_date`
field), reproduced here with a real stdio MCP server so the fix is proven
against the SDK's actual behaviour, not an assumption about its shape.

The fixture below raises `ToolError` rather than a bare `ValueError`. As of
MCP SDK 2.0, a bare exception from a tool body is treated as a crash: the
server withholds its text from the client on purpose (`Error executing tool
<name>`, nothing more) and only logs the real message server-side. Only
`ToolError` -- an "anticipated" failure -- keeps its message on the wire.
A well-behaved MCP server raises `ToolError` for exactly this kind of
validation rejection; this fixture models that server, not the client
change, since `mcp_client.py` has nothing to fix here -- it already raises
whatever text the server chose to send.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from oc8.agent.mcp_client import McpSession

pytestmark = pytest.mark.asyncio

_REJECTS_THE_CALL = """
try:
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

mcp = FastMCP("rejects")


@mcp.tool()
def search_records(model: str) -> str:
    "Mirrors Odoo rejecting an unknown field."
    raise ToolError("Invalid field 'sla_date' in request")


mcp.run()
"""


def _server(tmp_path: Path, source: str) -> tuple[str, list[str]]:
    script = tmp_path / "server.py"
    script.write_text(source)
    return sys.executable, [str(script)]


async def test_a_tool_level_rejection_raises_instead_of_returning_as_success(
    tmp_path: Path,
) -> None:
    command, args = _server(tmp_path, _REJECTS_THE_CALL)
    async with McpSession(command, args) as session:
        with pytest.raises(RuntimeError, match="Invalid field 'sla_date'"):
            await session.call("search_records", {"model": "helpdesk.ticket"})
