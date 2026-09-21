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
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from oc8.agent.mcp_client import McpSession, McpToolError

pytestmark = pytest.mark.asyncio

_REJECTS_THE_CALL = """
import json
import sys


def send(request_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    if request_id is None:
        continue

    method = request["method"]
    if method == "initialize":
        send(
            request_id,
            {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rejects", "version": "1.0"},
            },
        )
    elif method == "tools/list":
        send(
            request_id,
            {
                "tools": [
                    {
                        "name": "search_records",
                        "description": "Mirrors Odoo rejecting an unknown field.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"model": {"type": "string"}},
                            "required": ["model"],
                        },
                    }
                ]
            },
        )
    elif method == "tools/call":
        send(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "Invalid field 'sla_date' in request",
                    }
                ],
                "isError": True,
            },
        )
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
        with pytest.raises(Exception, match="Invalid field 'sla_date'") as caught:
            await session.call("search_records", {"model": "helpdesk.ticket"})

    exc = caught.value
    assert isinstance(exc, RuntimeError)
    assert isinstance(exc, McpToolError)
