"""Stdio MCP server entrypoint: `python -m mcp_bridge`.

The package is `mcp_bridge`, deliberately NOT `mcp`: this capa's root is on
PYTHONPATH (tool_pack.toml), so a local `mcp/` would shadow the installed MCP
SDK imported below.

Uses the mcp 2.0.0 constructor-callback API (`on_list_tools=`/`on_call_tool=`),
like capas/microsoft365. Results are JSON so the core's focus_spec can read a
record's title out of them.
"""

from __future__ import annotations

import asyncio
import json

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, ServerRequestContext

from .tools.courses import CALL_HANDLERS as COURSE_HANDLERS
from .tools.courses import TOOLS as COURSE_TOOLS
from .tools.grading import CALL_HANDLERS as GRADING_HANDLERS
from .tools.grading import TOOLS as GRADING_TOOLS
from .tools.people import CALL_HANDLERS as PEOPLE_HANDLERS
from .tools.people import TOOLS as PEOPLE_TOOLS

ALL_TOOLS: list[types.Tool] = [*COURSE_TOOLS, *PEOPLE_TOOLS, *GRADING_TOOLS]
ALL_HANDLERS = {**COURSE_HANDLERS, **PEOPLE_HANDLERS, **GRADING_HANDLERS}


async def _on_list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=ALL_TOOLS)


async def _on_call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    handler = ALL_HANDLERS.get(params.name)
    if handler is None:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
            is_error=True,
        )
    # An exception escaping this callback reaches the agent as a bare "Internal
    # server error"; a tool result with is_error=True carries the real reason
    # (an invalid token, a function missing from the external service, a bad
    # argument) to the agent and the run transcript.
    try:
        result = await handler(params.arguments or {})
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"ERROR: {exc}")], is_error=True
        )
    text = json.dumps(result, ensure_ascii=False, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server("moodle", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def _run() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
