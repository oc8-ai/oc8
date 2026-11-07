"""`guardrail_suggest` (D3a): the Copilot proposes one of the member's own
guardrails as a card. It writes nothing -- the member's Confirm on the card calls
their own PUT route."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agent.control_tools import GUARDRAIL_SUGGEST, ControlOutcome, offered_tools
from oc8.agent.engine import _authorize
from oc8.authz.pdp import Effect, ToolPolicy
from oc8.authz.permissions import SEAT_VIEWER
from oc8.chat.modes import MODES, WRITING_CONTROL_TOOLS, mode_refusal
from oc8.copilot.tools import execute_copilot_tool
from oc8.modelrouter import ToolCall
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import Seat, copilot_seat

GMAIL_FULL: dict[str, Any] = {"enabled": True, "read": True, "modify": True}


async def _run(db: AsyncSession, tenant: uuid.UUID, cop: m.Agent, seat: Seat) -> m.AgentRun:
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=cop.id,
        source="chat",
        state="running",
        task_id=seat.task_id,
        context={"chat_session_id": str(seat.session_id), "door": "web"},
    )
    db.add(run)
    await db.flush()
    return run


async def _agent(
    db: AsyncSession, tenant: uuid.UUID, *, tools: dict[str, Any], seat_for: uuid.UUID | None
) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name=f"D{uuid.uuid4().hex[:6]}", frame={"tools": tools})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Nora", narrowing={}, definition={}
    )
    db.add(agent)
    await db.flush()
    if seat_for is not None:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=seat_for,
                department_id=dept.id,
                seat_role=SEAT_VIEWER,
            )
        )
        await db.flush()
    return agent


async def _suggest(
    db: AsyncSession, tenant: uuid.UUID, seat: Seat, cop: m.Agent, args: dict[str, Any]
) -> ControlOutcome | None:
    task = await db.get(m.Task, seat.task_id)
    assert task is not None
    run = await _run(db, tenant, cop, seat)
    return await execute_copilot_tool(
        db,
        tenant_id=tenant,
        agent=cop,
        task=task,
        run_id=run.id,
        tc=ToolCall(id="g", name="guardrail_suggest", arguments=args),
    )


async def _rows(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(m.MemberGuardrail)) or 0)


async def test_a_valid_suggestion_renders_the_card_and_writes_nothing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        agent = await _agent(db, tenant, tools={"gmail": GMAIL_FULL}, seat_for=seat.member_id)
        out = await _suggest(
            db,
            tenant,
            seat,
            cop,
            {"agent_id": str(agent.id), "tool_key": "gmail", "level": "ask_first"},
        )
        assert out is not None and out.rendered_component is not None
        assert not out.output.startswith("ERROR"), out.output
        assert out.rendered_component == {
            "component_key": "guardrail_card",
            "props": {
                "agentId": str(agent.id),
                "agentName": "Nora",
                "toolKey": "gmail",
                "level": "ask_first",
                "baseLevel": "allowed",
            },
        }
        assert await _rows(db) == 0


async def test_an_agent_the_member_cannot_reach_is_refused_without_a_leak(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        hidden = await _agent(db, tenant, tools={"gmail": GMAIL_FULL}, seat_for=None)
        args = {"tool_key": "gmail", "level": "off"}
        a = await _suggest(db, tenant, seat, cop, {"agent_id": str(hidden.id), **args})
        b = await _suggest(db, tenant, seat, cop, {"agent_id": str(uuid.uuid4()), **args})
        assert a is not None and b is not None
        assert a.output.startswith("ERROR") and a.output == b.output
        assert a.rendered_component is None and await _rows(db) == 0


async def test_a_suggestion_that_would_widen_names_the_reason(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        agent = await _agent(
            db,
            tenant,
            tools={"gmail": {"enabled": False, "read": False, "modify": False}},
            seat_for=seat.member_id,
        )
        out = await _suggest(
            db,
            tenant,
            seat,
            cop,
            {"agent_id": str(agent.id), "tool_key": "gmail", "level": "read_only"},
        )
        assert out is not None and out.output.startswith("ERROR: ")
        assert len(out.output) > len("ERROR: ") and out.rendered_component is None
        assert await _rows(db) == 0


async def test_a_write_only_tool_can_ask_first_but_not_read_only(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        agent = await _agent(
            db,
            tenant,
            tools={"gmail": {"enabled": True, "read": False, "modify": True}},
            seat_for=seat.member_id,
        )
        args = {"agent_id": str(agent.id), "tool_key": "gmail"}
        ask = await _suggest(db, tenant, seat, cop, {**args, "level": "ask_first"})
        assert ask is not None and not ask.output.startswith("ERROR"), ask.output
        assert ask.rendered_component is not None
        read = await _suggest(db, tenant, seat, cop, {**args, "level": "read_only"})
        assert read is not None and read.output.startswith("ERROR: read only is not offered")
        assert read.rendered_component is None
        assert await _rows(db) == 0


async def test_allowed_is_not_a_suggestion_and_an_unknown_tool_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        agent = await _agent(db, tenant, tools={"gmail": GMAIL_FULL}, seat_for=seat.member_id)
        for tool, level in (("gmail", "allowed"), ("gmail", "nonsense"), ("nope", "off")):
            out = await _suggest(
                db, tenant, seat, cop, {"agent_id": str(agent.id), "tool_key": tool, "level": level}
            )
            assert out is not None and out.output.startswith("ERROR"), (tool, level)
            assert out.rendered_component is None


async def test_only_the_copilot_suggests(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        agent = await _agent(db, tenant, tools={"gmail": GMAIL_FULL}, seat_for=seat.member_id)
        out = await _suggest(
            db,
            tenant,
            seat,
            agent,
            {"agent_id": str(agent.id), "tool_key": "gmail", "level": "off"},
        )
        assert out is not None and out.output.startswith("ERROR")


def test_it_is_offered_and_allowed_in_plan_and_research() -> None:
    cop = m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Copilot",
        is_tenant_assistant=True,
        is_team_lead=True,
    )
    assert GUARDRAIL_SUGGEST.name not in WRITING_CONTROL_TOOLS
    common: dict[str, Any] = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}
    for key in ("plan", "research"):
        if key not in MODES:
            continue
        names = {
            t.name for t in offered_tools(cop, copilot_door="web", chat_mode=MODES[key], **common)
        }
        assert GUARDRAIL_SUGGEST.name in names, key
        assert mode_refusal(MODES[key], GUARDRAIL_SUGGEST.name, tool_scopes=None) is None
        effect = _authorize(
            cop,
            ToolCall(id="c", name="guardrail_suggest", arguments={}),
            frame={"tools": {}},
            tool_policies={"x": ToolPolicy(enabled=True, read=True, modify=True)},
            connection_key=None,
            tool_scopes=None,
            chat_mode=MODES[key],
        ).effect
        assert effect is Effect.ALLOW, key
