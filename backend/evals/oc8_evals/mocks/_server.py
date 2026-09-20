"""Stdio MCP server wiring shared by the mocks -- same constructor-callback
API the real bridges use (mcp 2.0.0 has no decorator API)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, ServerRequestContext

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


def serve(name: str, tools: list[types.Tool], handlers: dict[str, Handler]) -> None:
    async def _on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        handler = handlers.get(params.name)
        if handler is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
                is_error=True,
            )
        try:
            result = await handler(params.arguments or {})
        except Exception as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"ERROR: {exc}")], is_error=True
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(result))])

    server = Server(name, on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)

    async def _run() -> None:
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())
