"""fetch_url: lets an agent read a public web page or API endpoint named in its
own instructions (e.g. "check https://example.com/updates once a day"). Reuses
knowledge.connectors.fetcher.safe_fetch, so the same SSRF guard (loopback,
private, link-local, reserved, multicast, unspecified, non-global addresses
all refused) applies here too -- this tool can never reach anything on the
agent's own isolated network, only the public internet."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.control_tools import FETCH_URL, execute_control_tool, offered_tools
from oc8.authz.pdp import Decision, Effect
from oc8.knowledge.connectors.base import ConnectorError
from oc8.modelrouter import NeutralTool, ToolCall

MCP_TOOLS: list[NeutralTool] = []


def _agent() -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        status="idle",
        definition={},
        presentation={},
    )


async def _dept_agent_task(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task]:
    dept = m.Department(tenant_id=tenant, name="Content", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Blog Writer",
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Summarise Hacker News",
        state="in_progress",
        delegation_depth=0,
    )
    db.add(task)
    await db.flush()
    return agent, task


def test_offered_to_any_agent_unconditionally() -> None:
    names = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    assert FETCH_URL.name in names


@pytest.mark.asyncio
async def test_fetches_and_returns_the_page_content(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_safe_fetch(url: str, **_: Any) -> tuple[str, str]:
        assert url == "https://news.ycombinator.com/"
        return "<html>Top story: ...</html>", "text/html"

    monkeypatch.setattr("oc8.knowledge.connectors.fetcher.safe_fetch", fake_safe_fetch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name="fetch_url",
                arguments={"url": "https://news.ycombinator.com/"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
    assert outcome is not None
    assert "Top story" in outcome.output
    assert outcome.output.startswith("[text/html]")


@pytest.mark.asyncio
async def test_a_blocked_address_is_a_plain_error_not_an_exception(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_safe_fetch(url: str, **_: Any) -> tuple[str, str]:
        from oc8.net.outbound import OutboundBlocked

        try:
            raise OutboundBlocked("blocked host 'intranet.example' -> 10.0.0.5")
        except OutboundBlocked as exc:
            raise ConnectorError(str(exc)) from exc

    monkeypatch.setattr("oc8.knowledge.connectors.fetcher.safe_fetch", fake_safe_fetch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name="fetch_url",
                arguments={"url": "http://169.254.169.254/latest/meta-data/"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
    # The guard's own text names the internal address a host resolved to;
    # the model gets a plain sentence instead.
    assert "10.0.0.5" not in outcome.output
    assert "not reachable" in outcome.output


@pytest.mark.asyncio
async def test_a_missing_url_argument_is_a_plain_error(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name="fetch_url", arguments={}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_a_large_page_is_truncated_not_refused(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = "x" * 30_000

    async def fake_safe_fetch(url: str, **_: Any) -> tuple[str, str]:
        return big, "text/plain"

    monkeypatch.setattr("oc8.knowledge.connectors.fetcher.safe_fetch", fake_safe_fetch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1", name="fetch_url", arguments={"url": "https://example.com/big"}
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
    assert outcome is not None
    assert "[truncated" in outcome.output
    assert len(outcome.output) < 30_000
