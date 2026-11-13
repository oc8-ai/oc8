"""Stdio MCP server entrypoint: `python -m mcp_bridge`. Aggregates every
`tools/*.py` module's TOOLS/CALL_HANDLERS -- Tasks 8-12 each add one import
and one dict-merge line here, nothing else.

The package is `mcp_bridge`, deliberately NOT `mcp`: this plugin's own root is
on PYTHONPATH (see tool_pack.toml), so a local package named `mcp/` would
shadow the installed MCP SDK that the three `import mcp.*` lines below need,
and the bridge would import itself and die on startup.

Uses mcp 2.0.0's constructor-callback API (`on_list_tools=`/`on_call_tool=`),
not the older `@server.list_tools()`/`@server.call_tool()` decorators from
earlier SDK versions -- this repo's locked `mcp` dependency is 2.0.0, whose
`Server` class has no such decorators at all.
"""

from __future__ import annotations

import asyncio
import json

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, ServerRequestContext

from .tools.calendar import CALL_HANDLERS as CALENDAR_HANDLERS
from .tools.calendar import TOOLS as CALENDAR_TOOLS
from .tools.files import CALL_HANDLERS as FILES_HANDLERS
from .tools.files import TOOLS as FILES_TOOLS
from .tools.mail import CALL_HANDLERS as MAIL_HANDLERS
from .tools.mail import TOOLS as MAIL_TOOLS
from .tools.office import CALL_HANDLERS as OFFICE_HANDLERS
from .tools.office import TOOLS as OFFICE_TOOLS
from .tools.powerpoint import CALL_HANDLERS as POWERPOINT_HANDLERS
from .tools.powerpoint import TOOLS as POWERPOINT_TOOLS
from .tools.teams import CALL_HANDLERS as TEAMS_HANDLERS
from .tools.teams import TOOLS as TEAMS_TOOLS

ALL_TOOLS: list[types.Tool] = [*MAIL_TOOLS, *CALENDAR_TOOLS, *FILES_TOOLS, *OFFICE_TOOLS, *TEAMS_TOOLS, *POWERPOINT_TOOLS]
ALL_HANDLERS = {**MAIL_HANDLERS, **CALENDAR_HANDLERS, **FILES_HANDLERS, **OFFICE_HANDLERS, **TEAMS_HANDLERS, **POWERPOINT_HANDLERS}


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
    # An un-typed exception escaping this callback is converted by the mcp 2.0.0
    # runner into a generic JSON-RPC INTERNAL_ERROR ("handler internals never
    # reach the wire"), so the agent and the run transcript saw "Internal server
    # error" instead of "Microsoft Graph rejected the credentials (HTTP 403) --
    # check the app registration's Graph permissions and admin consent". Which
    # is the single most likely failure this pack has. A tool *result* carrying
    # is_error=True is the SDK's own way to say "this call failed, here is why"
    # -- the same shape the unknown-tool branch above already uses.
    try:
        result = await handler(params.arguments or {})
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"ERROR: {exc}")], is_error=True
        )
    # JSON, not str(): record links parse the output, and str(dict) is Python
    # repr (single quotes, True/None) that no JSON parser reads. A bare string
    # (document text) stays raw: JSON-quoting it would hand the model an escaped blob.
    text = (
        result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    )
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server("microsoft365", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def _run() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
