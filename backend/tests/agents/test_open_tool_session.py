"""Tests for open_tool_session factory and resolve_auth_header."""

from __future__ import annotations

import pytest

from oc8.agent.mcp_client import HttpToolSession, McpSession, open_tool_session, resolve_auth_header


def test_resolve_auth_header_present() -> None:
    cfg = {"auth_header_name": "Authorization"}
    env = {"Authorization": "Bearer xyz"}
    assert resolve_auth_header(cfg, env) == {"Authorization": "Bearer xyz"}


def test_resolve_auth_header_absent_when_not_declared() -> None:
    assert resolve_auth_header({}, {"Authorization": "Bearer xyz"}) == {}


def test_resolve_auth_header_absent_when_env_missing_value() -> None:
    assert resolve_auth_header({"auth_header_name": "Authorization"}, {}) == {}


@pytest.mark.asyncio
async def test_open_tool_session_stdio_returns_mcp_session() -> None:
    session = await open_tool_session(transport="stdio", command="true", args=[])
    assert isinstance(session, McpSession)


@pytest.mark.asyncio
async def test_open_tool_session_http_returns_mcp_session_with_url() -> None:
    session = await open_tool_session(transport="http", server_url="http://example.invalid/mcp")
    assert isinstance(session, McpSession)


@pytest.mark.asyncio
async def test_open_tool_session_manual_http_returns_http_tool_session() -> None:
    session = await open_tool_session(
        transport="manual_http",
        server_url="http://example.invalid",
        http_tools=[{"name": "t", "description": "", "method": "GET", "url_template": "/"}],
    )
    assert isinstance(session, HttpToolSession)
