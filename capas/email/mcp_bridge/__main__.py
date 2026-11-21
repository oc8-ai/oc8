"""Stdio MCP server entrypoint: `python -m mcp_bridge`.

The package is `mcp_bridge`, not `mcp`: this capa's own root is on PYTHONPATH
(tool_pack.toml), so a local `mcp/` would shadow the installed MCP SDK the
imports below need. Uses mcp 2's constructor-callback API, as microsoft365's
bridge does.
"""

from __future__ import annotations

import asyncio
import json

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, ServerRequestContext

from .mailbox import MailError
from .tools import CALL_HANDLERS, TOOLS


async def _on_list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


def _result_text(result: object) -> str:
    """Non-strings as JSON, so record links parse them; mail content is always
    inside a JSON value, never bare text core could mistake for records."""
    if not isinstance(result, str):
        return json.dumps(result, ensure_ascii=False, default=str)
    return json.dumps(result, ensure_ascii=False)


async def _on_call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    handler = CALL_HANDLERS.get(params.name)
    if handler is None:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
            is_error=True,
        )
    # A failure goes back as an error result with our own wording (MailError
    # carries no secret); anything else is named by its class only.
    try:
        result = await handler(params.arguments or {})
    except MailError as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"ERROR: {exc}")], is_error=True
        )
    except Exception as exc:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=f"ERROR: the mail tool failed ({type(exc).__name__})"
                )
            ],
            is_error=True,
        )
    return types.CallToolResult(content=[types.TextContent(type="text", text=_result_text(result))])


server = Server("email", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def _run() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
