"""Stdio MCP server entrypoint: `python -m mcp_bridge`. Aggregates every
`tools/*.py` module's TOOLS/CALL_HANDLERS -- Tasks 6-10 each add one import
and one dict-merge line here, nothing else.

The package is `mcp_bridge`, deliberately NOT `mcp`: this plugin's own root is
on PYTHONPATH (see tool_pack.toml), so a local package named `mcp/` would
shadow the installed MCP SDK that the three `import mcp.*` lines below need,
and the bridge would import itself and die on startup.

Uses mcp 2.0.0's constructor-callback API (`on_list_tools=`/`on_call_tool=`),
not the older `@server.list_tools()`/`@server.call_tool()` decorators -- this
repo's locked `mcp` dependency is 2.0.0, whose `Server` class has no such
decorators at all (confirmed directly against the installed package during the
Microsoft 365 plugin's Task 7; this file mirrors that confirmed-correct shape).
"""

from __future__ import annotations

import asyncio
import json

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, ServerRequestContext

from .tools.calendar import CALL_HANDLERS as CALENDAR_HANDLERS
from .tools.calendar import TOOLS as CALENDAR_TOOLS
from .tools.docs import CALL_HANDLERS as DOCS_HANDLERS
from .tools.docs import TOOLS as DOCS_TOOLS
from .tools.drive import CALL_HANDLERS as DRIVE_HANDLERS
from .tools.drive import TOOLS as DRIVE_TOOLS
from .tools.gmail import CALL_HANDLERS as GMAIL_HANDLERS
from .tools.gmail import TOOLS as GMAIL_TOOLS
from .tools.sheets import CALL_HANDLERS as SHEETS_HANDLERS
from .tools.sheets import TOOLS as SHEETS_TOOLS
from .tools.slides import CALL_HANDLERS as SLIDES_HANDLERS
from .tools.slides import TOOLS as SLIDES_TOOLS

ALL_TOOLS: list[types.Tool] = [
    *GMAIL_TOOLS,
    *CALENDAR_TOOLS,
    *DRIVE_TOOLS,
    *DOCS_TOOLS,
    *SHEETS_TOOLS,
    *SLIDES_TOOLS,
]
ALL_HANDLERS = {
    **GMAIL_HANDLERS,
    **CALENDAR_HANDLERS,
    **DRIVE_HANDLERS,
    **DOCS_HANDLERS,
    **SHEETS_HANDLERS,
    **SLIDES_HANDLERS,
}


async def _on_list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=ALL_TOOLS)


def _result_text(result: object) -> str:
    """Tool output as the model sees it. Non-strings are JSON (record links parse
    them, and str(dict) is Python repr no parser reads). A bare string stays raw,
    unless it is itself valid JSON (document text can be anything): then it goes
    out as a quoted JSON string, so core never parses attacker-authored file
    content as records."""
    if not isinstance(result, str):
        return json.dumps(result, ensure_ascii=False, default=str)
    try:
        json.loads(result)
    except ValueError:
        return result
    return json.dumps(result, ensure_ascii=False)


async def _on_call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    handler = ALL_HANDLERS.get(params.name)
    if handler is None:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
            is_error=True,
        )
    try:
        result = await handler(params.arguments or {})
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"ERROR: {exc}")],
            is_error=True,
        )
    text = _result_text(result)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server("google_workspace", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def _run() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
