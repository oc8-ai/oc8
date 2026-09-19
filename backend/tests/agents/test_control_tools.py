"""The control-plane tools the core itself owns, shared by both runtimes.

These are the tools that are NOT a connection's MCP tools: remember something,
ask the operator, delegate, invoke a skill. They used to be defined inline in
engine.py's run loop, so the isolated runtime offered none of them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import (
    CONTROL_TOOL_NAMES,
    ControlOutcome,
    execute_control_tool,
    offered_tools,
)
from oc8.authz.pdp import Decision, Effect
from oc8.modelrouter import NeutralTool, ToolCall
from oc8.skills.runtime import LoadedSkill
from oc8.skills.schema import parse_definition

MCP_TOOLS = [
    NeutralTool(name="create_record", description="", parameters={}),
    NeutralTool(name="read_record", description="", parameters={}),
    NeutralTool(name="send_email", description="", parameters={}),
]


def _agent(*, is_team_lead: bool = False, is_tenant_assistant: bool = False) -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        status="idle",
        definition={},
        presentation={},
        is_team_lead=is_team_lead,
        is_tenant_assistant=is_tenant_assistant,
    )


def _skill(tool_name: str, *, requires: list[str]) -> LoadedSkill:
    definition: dict[str, Any] = {
        "oc8_skill": 1,
        "id": "sk-x",
        "version": "1.0.0",
        "instruction": "Do the thing.",
        "requires": {"tools": [{"tool": t, "rights": ["read"]} for t in requires], "kbs": []},
        "guardrails": [],
    }
    return LoadedSkill(
        skill_id=uuid.uuid4(),
        skill_version_id=uuid.uuid4(),
        name="Skill X",
        description="d",
        tool_name=tool_name,
        definition=parse_definition(definition),
        creator_id=None,
    )


def test_every_agent_may_remember_and_ask() -> None:
    names = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    assert "memory_write" in names
    assert "ask_user" in names


def test_only_a_team_lead_is_offered_delegation() -> None:
    """Offering delegate_task to a non-lead would be offering a tool that
    _authorize denies on every call -- pure model confusion."""
    plain = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    lead = [
        t.name
        for t in offered_tools(
            _agent(is_team_lead=True), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS
        )
    ]
    assert "delegate_task" not in plain
    assert "delegate_task" in lead


def test_the_tenant_assistant_is_never_offered_ask_user() -> None:
    """ask_user parks a run waiting for an answer through the door the
    question arrived on -- a door the Assistant's Telegram side has no way to
    answer through at all. Withheld here so it can only ever answer, delegate,
    or say plainly it cannot help, never leave a human on any door waiting on
    a question that door cannot answer."""
    names = [
        t.name
        for t in offered_tools(
            _agent(is_team_lead=True, is_tenant_assistant=True),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=MCP_TOOLS,
        )
    ]
    assert "ask_user" not in names
    assert "delegate_task" in names
    assert "propose_change" in names


def test_an_ordinary_team_lead_still_gets_ask_user() -> None:
    """The withholding above is Assistant-specific -- an ordinary lead's own
    doors (web Chat, internal handoffs) can all answer a park."""
    names = [
        t.name
        for t in offered_tools(
            _agent(is_team_lead=True), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS
        )
    ]
    assert "ask_user" in names


def test_an_active_skill_narrows_the_connection_tools() -> None:
    """An active skill focuses the model on its own tools. OFFERING only --
    _authorize still checks the frame on every call, so this cannot widen."""
    skill = _skill("skill_x", requires=["read_record"])
    names = [
        t.name
        for t in offered_tools(
            _agent(), assigned_skills=[skill], active_skills=[skill], mcp_tools=MCP_TOOLS
        )
    ]
    assert "read_record" in names
    assert "create_record" not in names
    assert "send_email" not in names


def test_a_skill_requiring_nothing_available_falls_back_to_all_tools() -> None:
    """A skill whose required tools this connection does not have must not leave
    the model with no connection tools at all -- it would be unable to act."""
    skill = _skill("skill_x", requires=["nonexistent_tool"])
    names = [
        t.name
        for t in offered_tools(
            _agent(), assigned_skills=[skill], active_skills=[skill], mcp_tools=MCP_TOOLS
        )
    ]
    for t in MCP_TOOLS:
        assert t.name in names


def test_an_assigned_skill_stays_offered_once_active() -> None:
    """Withdrawing the tool the moment it activates would strand a model that
    re-checks its own tool list with an unknown tool name."""
    skill = _skill("skill_x", requires=["read_record"])
    names = [
        t.name
        for t in offered_tools(
            _agent(), assigned_skills=[skill], active_skills=[skill], mcp_tools=MCP_TOOLS
        )
    ]
    assert "skill_x" in names


def test_without_an_active_skill_all_connection_tools_are_offered() -> None:
    names = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    for t in MCP_TOOLS:
        assert t.name in names


def test_control_tool_names_matches_the_schemas() -> None:
    assert CONTROL_TOOL_NAMES == {
        "memory_write",
        "ask_user",
        "delegate_task",
        "request_decision",
        "search_knowledge",
        "search_memory",
        "render_component",
        "propose_change",
        "decide_approval",
        "read_reference_file",
        "read_instruction_file",
        "list_pending_approvals",
        "department_status",
        "agent_status",
        "budget_overview",
        "kpi_overview",
        "fetch_url",
        "todo_write",
        "write_output_file",
        "run_shell",
        "read_run_file",
    }


def test_render_component_is_offered_to_every_agent() -> None:
    """Withholding it would not be a real gate -- the ComponentGrant check
    inside execute_control_tool is the gate; offering only hides, and "a
    grant that only hides is not a grant that holds" (see pdp.py)."""
    names = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    assert "render_component" in names


def test_search_knowledge_is_withheld_without_a_knowledge_base_grant() -> None:
    """Offering search_knowledge to an agent with no KB grant at all would be
    offering a tool that can only ever answer "nothing in the knowledge base"
    -- noise, not a capability. Defaults to withheld: has_knowledge defaults
    to False, matching every pre-existing offered_tools() call site until it
    is updated to pass the real value."""
    names = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    assert "search_knowledge" not in names


def test_search_knowledge_is_offered_once_a_knowledge_base_is_granted() -> None:
    names = [
        t.name
        for t in offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=MCP_TOOLS,
            has_knowledge=True,
        )
    ]
    assert "search_knowledge" in names


def test_read_instruction_file_is_withheld_without_any_attached_file() -> None:
    """Same reasoning as search_knowledge above: an agent with no file
    attached to its own Instructions has nothing this tool could resolve.
    Defaults to withheld -- has_instruction_files defaults to False, matching
    every pre-existing offered_tools() call site until it passes the real
    value."""
    names = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    assert "read_instruction_file" not in names


def test_read_instruction_file_is_offered_once_a_file_is_attached() -> None:
    names = [
        t.name
        for t in offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=MCP_TOOLS,
            has_instruction_files=True,
        )
    ]
    assert "read_instruction_file" in names


# ------------------------------------------------------------------ execution


async def _dept_agent_task(
    db: Any, tenant: uuid.UUID, *, is_team_lead: bool = False, depth: int = 0
) -> tuple[m.Agent, m.Task]:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        definition={},
        presentation={},
        narrowing={},
        is_team_lead=is_team_lead,
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Erstelle ein Angebot",
        state="in_progress",
        delegation_depth=depth,
    )
    db.add(task)
    await db.flush()
    return agent, task


@pytest.mark.asyncio
async def test_ask_user_asks_to_suspend(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name="ask_user", arguments={"question": "Welches Konto?"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert outcome.suspend == "waiting_for_input"
    assert outcome.output == "Welches Konto?"


@pytest.mark.asyncio
async def test_an_empty_question_is_a_model_error_not_a_suspend(app_session: Any) -> None:
    """Suspending on an empty question would park the run on a question no human
    can answer -- it is the model that erred, so tell the model."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name="ask_user", arguments={"question": "   "}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert outcome.suspend is None
    assert outcome.output.startswith("ERROR:")


@pytest.mark.asyncio
async def test_memory_write_records_and_reports_the_id(app_session: Any) -> None:
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
                name="memory_write",
                arguments={"tier": "agent", "content": "Kunde zahlt per Rechnung."},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
        assert "memory recorded" in outcome.output
        rows = (
            await db.execute(
                m.MemoryRecord.__table__.select().where(m.MemoryRecord.tenant_id == tenant)
            )
        ).fetchall()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_denied_memory_write_reaches_the_model_as_an_error(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1", name="memory_write", arguments={"tier": "company", "content": "x"}
            ),
            decision=Decision(Effect.DENY, "company tier not permitted"),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
        assert outcome.output == "ERROR: company tier not permitted"
        rows = (
            await db.execute(
                m.MemoryRecord.__table__.select().where(m.MemoryRecord.tenant_id == tenant)
            )
        ).fetchall()
        assert rows == []


@pytest.mark.asyncio
async def test_delegate_task_creates_a_sub_run_the_caller_must_publish(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        mate = m.Agent(
            tenant_id=tenant,
            department_id=lead.department_id,
            name="Rico",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(mate)
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="c1",
                name="delegate_task",
                arguments={"agent_id": str(mate.id), "task_text": "Ruf den Kunden an"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
        assert outcome.pending_run is not None
        assert "Rico" in outcome.output
        sub = await db.get(m.AgentRun, outcome.pending_run)
        assert sub is not None
        # The sub-run carries the hop count, so the depth cap can actually bite.
        assert sub.context["delegation_depth"] == 1
        assert sub.context["parent_task_id"] == str(task.id)


@pytest.mark.asyncio
async def test_delegate_task_carries_the_chat_origin_onto_the_sub_run(app_session: Any) -> None:
    """A wake-up all the way back at the top of a delegation chain can only be
    recognised as a CHAT continuation (executor._maybe_wake_parent) if every
    hop in between carried the origin forward. Without this a chat-originated
    delegation's eventual answer never reached the user on any channel."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        mate = m.Agent(
            tenant_id=tenant,
            department_id=lead.department_id,
            name="Rico",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(mate)
        executing_run = m.AgentRun(
            tenant_id=tenant,
            agent_id=lead.id,
            source="chat",
            context={
                "chat_session_id": "aaaa-bbbb",
                "chat_channel": "teams",
                "chat_channel_external_id": "tg-7",
            },
        )
        db.add(executing_run)
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="c1",
                name="delegate_task",
                arguments={"agent_id": str(mate.id), "task_text": "Ruf den Kunden an"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=executing_run.id,
        )
        assert outcome is not None
        assert outcome.pending_run is not None
        sub = await db.get(m.AgentRun, outcome.pending_run)
        assert sub is not None
        assert sub.context["chat_session_id"] == "aaaa-bbbb"
        assert sub.context["chat_channel"] == "teams"
        assert sub.context["chat_channel_external_id"] == "tg-7"


@pytest.mark.asyncio
async def test_delegate_task_without_a_chat_origin_carries_nothing(app_session: Any) -> None:
    """A plain, non-chat run (cron, manual, webhook, ...) must not spuriously
    stamp chat context onto a sub-run it never had."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        mate = m.Agent(
            tenant_id=tenant,
            department_id=lead.department_id,
            name="Rico",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(mate)
        executing_run = m.AgentRun(tenant_id=tenant, agent_id=lead.id, source="cron", context={})
        db.add(executing_run)
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="c1",
                name="delegate_task",
                arguments={"agent_id": str(mate.id), "task_text": "Ruf den Kunden an"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=executing_run.id,
        )
        assert outcome is not None
        assert outcome.pending_run is not None
        sub = await db.get(m.AgentRun, outcome.pending_run)
        assert sub is not None
        assert "chat_session_id" not in sub.context
        assert "chat_channel" not in sub.context
        assert "chat_channel_external_id" not in sub.context


@pytest.mark.asyncio
async def test_delegation_to_another_department_is_refused(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        other = m.Department(tenant_id=tenant, name="Buchhaltung", frame={})
        db.add(other)
        await db.flush()
        stranger = m.Agent(
            tenant_id=tenant,
            department_id=other.id,
            name="Fremd",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(stranger)
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="c1",
                name="delegate_task",
                arguments={"agent_id": str(stranger.id), "task_text": "x"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
        assert outcome.pending_run is None
        assert "your own department" in outcome.output


# --------------------------------------- the Assistant's cross-department seam


async def _lead_and_stranger(
    db: Any, tenant: uuid.UUID, *, is_tenant_assistant: bool
) -> tuple[m.Agent, m.Task, m.Agent]:
    """A team lead with an open task, plus an agent in a DIFFERENT department."""
    lead, task = await _dept_agent_task(db, tenant, is_team_lead=True)
    lead.is_tenant_assistant = is_tenant_assistant
    other = m.Department(tenant_id=tenant, name="Buchhaltung", frame={})
    db.add(other)
    await db.flush()
    stranger = m.Agent(
        tenant_id=tenant,
        department_id=other.id,
        name="Fremd",
        status="idle",
        definition={},
        presentation={},
    )
    db.add(stranger)
    await db.flush()
    return lead, task, stranger


async def _human_behind(
    db: Any,
    tenant: uuid.UUID,
    task: m.Task,
    *,
    all_departments: bool = False,
    seat_in: uuid.UUID | None = None,
    seat_revoked: bool = False,
) -> m.OrgMember:
    """The person whose chat session opened `task` -- which is the ONLY way the
    guard can learn whose access to measure the delegation against."""
    member = m.OrgMember(
        tenant_id=tenant,
        subject=f"sub-{uuid.uuid4()}",
        subject_uuid=uuid.uuid4(),
        all_departments=all_departments,
    )
    db.add(member)
    await db.flush()
    if seat_in is not None:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=member.id,
                department_id=seat_in,
                seat_role="dept_approver",
                revoked_at=dt.datetime.now(dt.UTC) if seat_revoked else None,
            )
        )
    db.add(
        m.ChatSession(
            tenant_id=tenant,
            agent_id=task.assigned_agent_id,
            member_id=member.id,
            task_id=task.id,
        )
    )
    await db.flush()
    return member


async def _try_delegate(
    db: Any,
    tenant: uuid.UUID,
    lead: m.Agent,
    task: m.Task,
    target: m.Agent,
    run_id: uuid.UUID | None = None,
) -> ControlOutcome:
    """`run_id` is the run this delegation executes UNDER -- the one whose
    context carries the acting token's role claim. Several chat runs share one
    task, so passing the task alone is not enough to say whose claim applies.
    """
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=lead,
        task=task,
        tc=ToolCall(
            id="c1",
            name="delegate_task",
            arguments={"agent_id": str(target.id), "task_text": "Tickets abarbeiten"},
        ),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
        run_id=run_id,
    )
    assert outcome is not None
    return outcome


@pytest.mark.asyncio
async def test_assistant_may_delegate_cross_department_when_member_has_access(
    app_session: Any,
) -> None:
    """The Assistant is the one agent allowed out of its own department -- and
    only as far as the human behind the chat could have gone themselves."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        await _human_behind(db, tenant, task, all_departments=True)
        outcome = await _try_delegate(db, tenant, lead, task, stranger)
        assert outcome.pending_run is not None
        assert "delegated to Fremd" in outcome.output


@pytest.mark.asyncio
async def test_a_live_seat_in_the_target_department_is_enough(app_session: Any) -> None:
    """all_departments is not the only key: a live seat in THAT department is
    the same standing the approval fan-out already honours."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        await _human_behind(db, tenant, task, seat_in=stranger.department_id)
        outcome = await _try_delegate(db, tenant, lead, task, stranger)
        assert outcome.pending_run is not None


@pytest.mark.asyncio
async def test_a_revoked_seat_does_not_let_the_assistant_cross(app_session: Any) -> None:
    """A revoked seat is history, not access -- the row still exists, so a
    membership check that forgot `revoked_at` would read as access."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        await _human_behind(db, tenant, task, seat_in=stranger.department_id, seat_revoked=True)
        outcome = await _try_delegate(db, tenant, lead, task, stranger)
        assert outcome.pending_run is None
        # Refused on the HUMAN's access, not on the blanket same-department
        # rule -- the Assistant is past that one, so only this can be refusing.
        assert "does not have access" in outcome.output


@pytest.mark.asyncio
async def test_assistant_may_not_delegate_where_the_member_has_no_access(
    app_session: Any,
) -> None:
    """The escalation this guard exists to stop: somebody who may only see
    Vertrieb asking the Assistant to start work in Buchhaltung."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        # A seat, but in the WRONG department -- so the member exists and is
        # live, and only the department predicate can refuse this.
        await _human_behind(db, tenant, task, seat_in=lead.department_id)
        outcome = await _try_delegate(db, tenant, lead, task, stranger)
        assert outcome.pending_run is None
        # Refused on the HUMAN's access, not on the blanket same-department
        # rule -- the Assistant is past that one, so only this can be refusing.
        assert "does not have access" in outcome.output


@pytest.mark.asyncio
async def test_the_assistant_is_refused_when_nobody_is_behind_the_run(app_session: Any) -> None:
    """Fail closed: a delegated or scheduled run has no chat session, so there
    is no human whose access could authorise leaving the department. Opening
    that up 'because there is nobody to check' is the fail-open shape."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        outcome = await _try_delegate(db, tenant, lead, task, stranger)
        assert outcome.pending_run is None
        # Refused on the HUMAN's access, not on the blanket same-department
        # rule -- the Assistant is past that one, so only this can be refusing.
        assert "does not have access" in outcome.output


async def _builtin_role(db: Any, tenant: uuid.UUID, name: str) -> m.Role:
    """A builtin role row, the only way the code table actually grants a
    tenant-wide permission (`_role_and_permissions` reads a builtin's grants
    from `BUILTIN_ROLE_PERMISSIONS`, and `approval:decide_any` is not
    delegatable, so hand-written RolePermission rows would be intersected
    away). Same shape as `tests/authz/test_authority.py`'s own `_role` helper.
    """
    role = m.Role(tenant_id=tenant, name=name, builtin=True, kind="human")
    db.add(role)
    await db.flush()
    return role


@pytest.mark.asyncio
async def test_role_derived_tenant_wide_reach_lets_the_assistant_cross(app_session: Any) -> None:
    """A member with NEITHER `all_departments` NOR a seat -- which is exactly
    what `authz.scope._upsert_member` mints on first sight -- but who holds a
    role carrying `approval:decide_any`.

    The guard used to read the seat tables by hand and know only about the row
    terms, so on a fresh tenant it refused everybody: nobody could
    cross-department-delegate at all, and it only appeared to work in the dev
    tenant because that one member happened to carry `all_departments=True`.
    """
    from oc8.authz.permissions import ORG_ADMIN

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        member = await _human_behind(db, tenant, task)
        assert member.all_departments is False
        member.role_id = (await _builtin_role(db, tenant, ORG_ADMIN)).id
        await db.flush()
        outcome = await _try_delegate(db, tenant, lead, task, stranger)
        assert outcome.pending_run is not None, outcome.output


@pytest.mark.asyncio
async def test_a_narrow_role_still_cannot_reach_an_out_of_scope_department(
    app_session: Any,
) -> None:
    """The other half: a REAL role with REAL permissions (`operator` holds
    `run:start`, `run:control`, `approval:decide`, a pile of `:view`s) that
    simply is not tenant-wide. A guard that read "has a role" as "may reach
    anything" would pass this one too."""
    from oc8.authz.permissions import OPERATOR

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        member = await _human_behind(db, tenant, task, seat_in=lead.department_id)
        member.role_id = (await _builtin_role(db, tenant, OPERATOR)).id
        await db.flush()
        outcome = await _try_delegate(db, tenant, lead, task, stranger)
        assert outcome.pending_run is None
        assert "does not have access" in outcome.output


@pytest.mark.asyncio
async def test_the_acting_tokens_role_carries_reach_for_a_member_with_no_assigned_role(
    app_session: Any,
) -> None:
    """`role_id IS NULL` does not mean "no permissions" -- everywhere else in
    this system it means "the token decides" (`authz.authority`'s FLOOR). A run
    has no token, so `chat/service.send_message` records the claim on the run
    and `_acting_token_role` reads it back. Without that, an administrator on a
    fresh tenant -- who has no assigned role and no seats, because nothing has
    ever written either -- could not cross-department-delegate."""
    from oc8.authz.permissions import ORG_ADMIN

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        member = await _human_behind(db, tenant, task)
        assert member.role_id is None
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=lead.id,
            task_id=task.id,
            source="chat",
            state="running",
            context={"operator_role": ORG_ADMIN},
        )
        db.add(run)
        await db.flush()
        outcome = await _try_delegate(db, tenant, lead, task, stranger, run_id=run.id)
        assert outcome.pending_run is not None, outcome.output


@pytest.mark.asyncio
async def test_a_narrow_acting_token_role_is_still_refused(app_session: Any) -> None:
    """The same run-context term, read the other way: an `operator` token is
    not tenant-wide, so recording it must not become a way in."""
    from oc8.authz.permissions import OPERATOR

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        await _human_behind(db, tenant, task)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=lead.id,
            task_id=task.id,
            source="chat",
            state="running",
            context={"operator_role": OPERATOR},
        )
        db.add(run)
        await db.flush()
        outcome = await _try_delegate(db, tenant, lead, task, stranger, run_id=run.id)
        assert outcome.pending_run is None
        assert "does not have access" in outcome.output


@pytest.mark.asyncio
async def test_the_claim_read_is_the_running_runs_own_not_the_newest_on_the_task(
    app_session: Any,
) -> None:
    """Every turn of one member's Assistant conversation -- web and Telegram
    alike -- shares one ChatSession and therefore one Task, so several chat
    runs sit on the same task at once.

    This used to resolve the role claim with "newest chat run on this task",
    which meant a LATER message could hand its claim to an EARLIER run that was
    still mid-delegation: one run's claim deciding another run's authorisation
    check. Here the in-flight run carries `operator` (no tenant-wide reach) and
    a newer run on the same task carries `org_admin`. The in-flight run must be
    refused on its OWN claim.
    """
    from oc8.authz.permissions import OPERATOR, ORG_ADMIN

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        await _human_behind(db, tenant, task)
        in_flight = m.AgentRun(
            tenant_id=tenant,
            agent_id=lead.id,
            task_id=task.id,
            source="chat",
            state="running",
            context={"operator_role": OPERATOR},
            created_at=dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.UTC),
        )
        newer = m.AgentRun(
            tenant_id=tenant,
            agent_id=lead.id,
            task_id=task.id,
            source="chat",
            state="queued",
            context={"operator_role": ORG_ADMIN},
            created_at=dt.datetime(2026, 1, 1, 10, 5, tzinfo=dt.UTC),
        )
        db.add_all([in_flight, newer])
        await db.flush()

        outcome = await _try_delegate(db, tenant, lead, task, stranger, run_id=in_flight.id)
        assert outcome.pending_run is None, (
            "the older, currently-executing run borrowed the newer run's claim"
        )
        assert "does not have access" in outcome.output

        # The other direction, so this cannot pass by simply always refusing:
        # the run that really does carry org_admin is still let through.
        allowed = await _try_delegate(db, tenant, lead, task, stranger, run_id=newer.id)
        assert allowed.pending_run is not None, allowed.output


@pytest.mark.asyncio
async def test_a_claim_on_a_run_of_another_tenant_is_ignored(app_session: Any) -> None:
    """The run id decides which claim is read, so the tenant is checked on the
    row rather than assumed from the id."""
    from oc8.authz.permissions import ORG_ADMIN

    tenant = uuid.uuid4()
    other_tenant = uuid.uuid4()
    async with app_session(other_tenant) as other_db:
        foreign = m.AgentRun(
            tenant_id=other_tenant,
            agent_id=uuid.uuid4(),
            source="chat",
            state="running",
            context={"operator_role": ORG_ADMIN},
        )
        other_db.add(foreign)
        await other_db.flush()
        foreign_id = foreign.id
        await other_db.commit()

    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=True)
        await _human_behind(db, tenant, task)
        outcome = await _try_delegate(db, tenant, lead, task, stranger, run_id=foreign_id)
        assert outcome.pending_run is None
        assert "does not have access" in outcome.output


@pytest.mark.asyncio
async def test_an_ordinary_team_lead_still_cannot_cross_departments(app_session: Any) -> None:
    """Regression: is_tenant_assistant=False (the default) is completely
    unaffected. Even with an unrestricted human behind the chat -- the exact
    setup that lets the Assistant through -- an ordinary lead never reaches the
    member check at all."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task, stranger = await _lead_and_stranger(db, tenant, is_tenant_assistant=False)
        await _human_behind(db, tenant, task, all_departments=True)
        outcome = await _try_delegate(db, tenant, lead, task, stranger)
        assert outcome.pending_run is None
        assert "your own department" in outcome.output


@pytest.mark.asyncio
async def test_the_depth_limit_deny_warns_an_operator(app_session: Any) -> None:
    """A runaway delegation chain must be visible to a human, not only to the
    model that tripped it."""
    from oc8.agent.control_tools import DEPTH_LIMIT_REASON

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="c1",
                name="delegate_task",
                arguments={"agent_id": str(uuid.uuid4()), "task_text": "x"},
            ),
            decision=Decision(Effect.DENY, DEPTH_LIMIT_REASON),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
        assert outcome.output.startswith("ERROR:")
        events = (
            await db.execute(
                m.ActivityEvent.__table__.select().where(
                    m.ActivityEvent.tenant_id == tenant,
                    m.ActivityEvent.status == "warning",
                )
            )
        ).fetchall()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_an_unrelated_delegation_deny_does_not_warn(app_session: Any) -> None:
    """Gated on the ACTUAL deny reason: an unrelated DENY on a task already at
    the cap must not mislabel the audit trail as a depth breach."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, task = await _dept_agent_task(db, tenant, is_team_lead=True, depth=5)
        await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(id="c1", name="delegate_task", arguments={"agent_id": "nope"}),
            decision=Decision(Effect.DENY, "invalid agent_id: 'nope'"),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        events = (
            await db.execute(
                m.ActivityEvent.__table__.select().where(
                    m.ActivityEvent.tenant_id == tenant,
                    m.ActivityEvent.status == "warning",
                )
            )
        ).fetchall()
        assert events == []


@pytest.mark.asyncio
async def test_invoking_a_skill_activates_it(app_session: Any) -> None:
    tenant = uuid.uuid4()
    skill = _skill("skill_x", requires=["read_record"])
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name="skill_x", arguments={}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[skill],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert outcome.activated_skill is skill
    assert "activated" in outcome.output


@pytest.mark.asyncio
async def test_re_invoking_an_active_skill_is_a_harmless_no_op(app_session: Any) -> None:
    tenant = uuid.uuid4()
    skill = _skill("skill_x", requires=["read_record"])
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name="skill_x", arguments={}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[skill],
            active_skills=[skill],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert outcome.activated_skill is None
    assert "already active" in outcome.output


@pytest.mark.asyncio
async def test_a_non_control_tool_is_not_handled_here(app_session: Any) -> None:
    """The dispatcher must say 'not mine' for a connection tool, so the caller
    routes it to the MCP server instead of silently swallowing it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name="create_record", arguments={}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
    assert outcome is None


@pytest.mark.parametrize("name", sorted(CONTROL_TOOL_NAMES))
def test_each_control_tool_declares_its_required_arguments(name: str) -> None:
    """A tool offered without a schema the model can satisfy is a tool the model
    will call wrongly. (The 5 read-only status tools intentionally have all
    optional parameters, so they are excepted from this check.)"""
    from oc8.agent.control_tools import CONTROL_TOOL_SCHEMAS

    schema = CONTROL_TOOL_SCHEMAS[name]
    assert schema.description
    # The status tools (list_pending_approvals, department_status, agent_status,
    # budget_overview, kpi_overview) intentionally have all optional parameters,
    # so they are allowed to have empty required lists.
    optional_tools = {
        "list_pending_approvals",
        "department_status",
        "agent_status",
        "budget_overview",
        "kpi_overview",
    }
    if name not in optional_tools:
        assert schema.parameters["required"]


@pytest.mark.asyncio
async def test_in_process_skill_instruction_lands_after_the_tool_result(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same invariant as the isolated path: a tool result must directly follow the
    assistant message that requested it. engine.py used to append an activated
    skill's instruction BEFORE the result, which a strict OpenAI-compatible
    provider rejects with 400 -- so the run died one step later. Never surfaced
    until an agent actually had a skill."""
    from oc8.agent.engine import run_agent
    from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result
    from oc8.skills.schema import parse_definition

    DEF = {
        "oc8_skill": 1,
        "id": "sk-x",
        "version": "1.0.0",
        "instruction": "FOLGE DIESEM VERFAHREN.",
        "requires": {"tools": [], "kbs": []},
        "guardrails": [],
    }

    seen: list[list[str]] = []

    class _InvokesSkillThenStops:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, req: Any) -> CompletionResult:
            self.calls += 1
            seen.append([msg.role for msg in req.messages])
            if self.calls == 1:
                return CompletionResult(
                    text="",
                    tool_calls=[ToolCall(id="c1", name="skill_sk_x", arguments={})],
                    usage=Usage(1, 1),
                    stop_reason="tool_use",
                    provider="ollama",
                    model="m",
                )
            return CompletionResult(
                text="fertig",
                tool_calls=[],
                usage=Usage(1, 1),
                stop_reason="stop",
                provider="ollama",
                model="m",
            )

        async def stream(self, req: Any) -> Any:
            yield chunk_from_result(await self.complete(req))

    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _InvokesSkillThenStops())

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            status="running",
            narrowing={},
            definition={},
            presentation={},
        )
        skill = m.Skill(tenant_id=tenant, name="X", description="d", author="t")
        db.add_all([agent, skill])
        await db.flush()
        version = m.SkillVersion(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition=DEF,
            artifact_hash=b"\x00" * 32,
        )
        db.add(version)
        await db.flush()
        skill.current_version_id = version.id
        db.add(
            m.SkillAssignment(
                tenant_id=tenant, agent_id=agent.id, skill_version_id=version.id, enabled=True
            )
        )
        await db.flush()
        _ = parse_definition(DEF)
        await run_agent(db, agent=agent, task_text="mach ein Angebot", tenant_id=tenant)

    # The SECOND model call is the one that would have been rejected.
    assert len(seen) >= 2, seen
    roles = seen[1]
    i = roles.index("assistant")
    assert roles[i + 1] == "tool", f"a tool result must follow its call directly, got {roles}"
    # And nothing follows it -- no injected system message to be misplaced.
    assert "system" not in roles[i + 1 :], f"no message may be inserted, got {roles}"


@pytest.mark.asyncio
async def test_render_component_is_refused_without_a_grant(app_session: Any) -> None:
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
                name="render_component",
                arguments={"component_key": "record_card", "props": {"title": "x"}},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert outcome.output.startswith("ERROR:")
    assert outcome.rendered_component is None


@pytest.mark.asyncio
async def test_render_component_rejects_an_unknown_component_key(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name="render_component",
                arguments={"component_key": "nope", "props": {}},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert outcome.output == "ERROR: no such component 'nope'"


@pytest.mark.asyncio
async def test_render_component_rejects_invalid_props(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name="render_component",
                arguments={"component_key": "record_card", "props": {"no_title": "x"}},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert outcome.output.startswith("ERROR: invalid props for 'record_card'")


@pytest.mark.asyncio
async def test_a_granted_agent_can_render_a_record_card(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name="render_component",
                arguments={
                    "component_key": "record_card",
                    "props": {"title": "Acme GmbH — 12.400 €"},
                },
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert not outcome.output.startswith("ERROR")
    assert outcome.rendered_component == {
        "component_key": "record_card",
        "props": {
            "title": "Acme GmbH — 12.400 €",
            "subtitle": None,
            "fields": [],
            "link_label": None,
            "link_url": None,
        },
    }


@pytest.mark.asyncio
async def test_a_department_wide_grant_covers_every_agent_in_it(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="department",
                grantee_id=agent.department_id,
            )
        )
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name="render_component",
                arguments={"component_key": "record_card", "props": {"title": "x"}},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert not outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_run_agent_publishes_a_rendered_component(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_component's effect is DATA on the outcome (see control_tools.py);
    it is run_agent's job to turn that into the realtime event the frontend
    actually consumes."""
    from oc8.agent.engine import run_agent
    from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result

    published: list[tuple[str, dict[str, Any]]] = []

    class _SpyBus:
        async def publish_event(
            self, tenant_id: Any, type_: str, data: dict[str, Any], **kw: Any
        ) -> None:
            published.append((type_, data))

    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _SpyBus())

    class _RendersThenStops:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, req: Any) -> CompletionResult:
            self.calls += 1
            if self.calls == 1:
                return CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="render_component",
                            arguments={"component_key": "record_card", "props": {"title": "x"}},
                        )
                    ],
                    usage=Usage(1, 1),
                    stop_reason="tool_use",
                    provider="ollama",
                    model="m",
                )
            return CompletionResult(
                text="fertig",
                tool_calls=[],
                usage=Usage(1, 1),
                stop_reason="stop",
                provider="ollama",
                model="m",
            )

        async def stream(self, req: Any) -> Any:
            yield chunk_from_result(await self.complete(req))

    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _RendersThenStops())

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task = await _dept_agent_task(db, tenant)
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context={})
        db.add(run)
        await db.flush()
        await run_agent(
            db, agent=agent, task_text="mach ein Angebot", tenant_id=tenant, run_id=run.id
        )

    assert (
        "run.component_rendered",
        {
            "run_id": str(run.id),
            "component_key": "record_card",
            "props": {
                "title": "x",
                "subtitle": None,
                "fields": [],
                "link_label": None,
                "link_url": None,
            },
        },
    ) in published


@pytest.mark.asyncio
async def test_run_agent_publishes_todos(app_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """todo_write's list is DATA on the outcome (see control_tools.py); like
    render_component above, it is run_agent's job to turn that into the
    realtime event an already-open Live Log tab actually consumes -- without
    it, `useRun`'s cache only ever reflects the todos present at its initial
    GET fetch (see frontend/src/lib/live/apply-event.ts)."""
    from oc8.agent.engine import run_agent
    from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result

    published: list[tuple[str, dict[str, Any]]] = []

    class _SpyBus:
        async def publish_event(
            self, tenant_id: Any, type_: str, data: dict[str, Any], **kw: Any
        ) -> None:
            published.append((type_, data))

    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _SpyBus())

    class _WritesTodoThenStops:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, req: Any) -> CompletionResult:
            self.calls += 1
            if self.calls == 1:
                return CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="todo_write",
                            arguments={"todos": [{"content": "Check it", "status": "pending"}]},
                        )
                    ],
                    usage=Usage(1, 1),
                    stop_reason="tool_use",
                    provider="ollama",
                    model="m",
                )
            return CompletionResult(
                text="fertig",
                tool_calls=[],
                usage=Usage(1, 1),
                stop_reason="stop",
                provider="ollama",
                model="m",
            )

        async def stream(self, req: Any) -> Any:
            yield chunk_from_result(await self.complete(req))

    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _WritesTodoThenStops())

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task = await _dept_agent_task(db, tenant)
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context={})
        db.add(run)
        await db.flush()
        await run_agent(
            db, agent=agent, task_text="drei Dinge erledigen", tenant_id=tenant, run_id=run.id
        )

    assert (
        "run.todos_updated",
        {"run_id": str(run.id), "todos": [{"content": "Check it", "status": "pending"}]},
    ) in published


@pytest.mark.asyncio
async def test_run_agent_returns_rendered_components_durably(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WS event above is live-only -- an unattended run (chat/cron) has
    no viewer to catch it. RunResult.rendered_components is the durable copy
    GET /runs/{id} actually reads back (see runtime/executor.py's
    merge_context call and api/v1/run.py's _to_dto)."""
    from oc8.agent.engine import run_agent
    from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result

    class _NoopBus:
        async def publish_event(self, *a: Any, **kw: Any) -> None:
            return None

    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _NoopBus())

    class _RendersThenStops:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, req: Any) -> CompletionResult:
            self.calls += 1
            if self.calls == 1:
                return CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="render_component",
                            arguments={"component_key": "record_card", "props": {"title": "x"}},
                        )
                    ],
                    usage=Usage(1, 1),
                    stop_reason="tool_use",
                    provider="ollama",
                    model="m",
                )
            return CompletionResult(
                text="fertig",
                tool_calls=[],
                usage=Usage(1, 1),
                stop_reason="stop",
                provider="ollama",
                model="m",
            )

        async def stream(self, req: Any) -> Any:
            yield chunk_from_result(await self.complete(req))

    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _RendersThenStops())

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task = await _dept_agent_task(db, tenant)
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context={})
        db.add(run)
        await db.flush()
        result = await run_agent(
            db, agent=agent, task_text="mach ein Angebot", tenant_id=tenant, run_id=run.id
        )

    assert result.rendered_components == [
        {
            "component_key": "record_card",
            "props": {
                "title": "x",
                "subtitle": None,
                "fields": [],
                "link_label": None,
                "link_url": None,
            },
        },
    ]


@pytest.mark.asyncio
async def test_a_direct_and_a_department_grant_can_coexist(app_session: Any) -> None:
    """ComponentGrant's own UniqueConstraint allows one row per grantee_type,
    so an agent can legitimately hold BOTH a direct grant and a department-wide
    grant for the same component_key at once -- this must not crash the grant
    check with MultipleResultsFound."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant)
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="department",
                grantee_id=agent.department_id,
            )
        )
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name="render_component",
                arguments={"component_key": "record_card", "props": {"title": "x"}},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
    assert not outcome.output.startswith("ERROR")


# ------------------------------------------ the Assistant's propose_change seam


async def _assistant_and_task(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task]:
    """The tenant Assistant with an open chat task."""
    assistant, task = await _dept_agent_task(db, tenant, is_team_lead=True)
    assistant.is_tenant_assistant = True
    await db.flush()
    return assistant, task


async def _propose(
    db: Any, tenant: uuid.UUID, agent: m.Agent, task: m.Task, arguments: dict[str, Any]
) -> ControlOutcome:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=agent,
        task=task,
        tc=ToolCall(id="c1", name="propose_change", arguments=arguments),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
    )
    assert outcome is not None
    return outcome


def test_propose_change_is_offered_only_to_the_assistant() -> None:
    """The tool IS the human-review boundary, so an ordinary agent must not
    even see it -- and the dispatch refuses it anyway if it calls it."""
    lead = _agent(is_team_lead=True)
    lead.is_tenant_assistant = False
    assert "propose_change" not in [
        t.name
        for t in offered_tools(lead, assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    lead.is_tenant_assistant = True
    assert "propose_change" in [
        t.name
        for t in offered_tools(lead, assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]


@pytest.mark.asyncio
async def test_propose_change_creates_a_draft_proposal_not_an_applied_one(
    app_session: Any,
) -> None:
    """The whole point: the Assistant may draft a structural change, never
    enact one. A proposal that came back already `applied` would mean the
    Assistant had restructured the tenant on its own say-so."""
    from oc8.copilot.models import CopilotProposal

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        outcome = await _propose(
            db,
            tenant,
            assistant,
            task,
            {
                "operation_type": "department.create",
                "payload": {"name": "Support EU", "goal": "", "icon": "building"},
            },
        )
        assert "proposal" in outcome.output.lower()
        proposal = (
            await db.execute(select(CopilotProposal).where(CopilotProposal.tenant_id == tenant))
        ).scalar_one()
        assert proposal.status == "draft"  # never applied automatically


@pytest.mark.asyncio
async def test_propose_change_rejects_an_unknown_operation_type(app_session: Any) -> None:
    """The capability registry is closed. A type outside it is refused as a
    model error rather than persisted as an unreviewable draft."""
    from oc8.copilot.models import CopilotProposal

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        outcome = await _propose(
            db, tenant, assistant, task, {"operation_type": "user.delete", "payload": {}}
        )
        assert outcome.output.startswith("ERROR")
        proposals = (
            (await db.execute(select(CopilotProposal).where(CopilotProposal.tenant_id == tenant)))
            .scalars()
            .all()
        )
        assert proposals == []


@pytest.mark.asyncio
async def test_propose_change_is_refused_for_a_non_assistant_agent(app_session: Any) -> None:
    """Withholding the tool from the offer list only hides it -- the dispatch
    has to refuse it too, or any agent that guesses the name can draft one."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        outcome = await _propose(
            db,
            tenant,
            agent,
            task,
            {
                "operation_type": "department.create",
                "payload": {"name": "Support EU"},
            },
        )
        assert outcome.output.startswith("ERROR")
        assert "Assistant" in outcome.output


@pytest.mark.asyncio
async def test_propose_change_needs_an_operation_type_and_a_payload(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        assert (await _propose(db, tenant, assistant, task, {"payload": {}})).output.startswith(
            "ERROR"
        )
        assert (
            await _propose(db, tenant, assistant, task, {"operation_type": "department.create"})
        ).output.startswith("ERROR")


@pytest.mark.asyncio
async def test_a_rejected_proposal_leaves_no_orphan_draft_behind(app_session: Any) -> None:
    """`create_proposal` flushes the proposal and its operations BEFORE
    `target_revision` checks the referenced row exists -- and a model inventing
    an agent id is the ordinary failure, not an exotic one. Without a savepoint
    the failed attempt survives as an operation-less draft in the human review
    queue: something a person is asked to approve that can never be applied.
    Different from the unknown-type case, which is refused before any db.add."""
    from oc8.copilot.models import CopilotOperation, CopilotProposal

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        outcome = await _propose(
            db,
            tenant,
            assistant,
            task,
            {
                "operation_type": "agent.mission.set",
                # Syntactically valid, refers to nothing -- so parse_operations
                # passes and target_revision is the one that refuses.
                "payload": {"agentId": str(uuid.uuid4()), "mission": "x"},
            },
        )
        assert outcome.output.startswith("ERROR")
        assert (
            await db.execute(select(CopilotProposal).where(CopilotProposal.tenant_id == tenant))
        ).scalars().all() == []
        assert (
            await db.execute(select(CopilotOperation).where(CopilotOperation.tenant_id == tenant))
        ).scalars().all() == []
        # The session must still be usable afterwards: a rolled-back savepoint
        # is the point, a poisoned transaction would end the whole run.
        assert (
            await _propose(
                db,
                tenant,
                assistant,
                task,
                {"operation_type": "department.create", "payload": {"name": "Support EU"}},
            )
        ).output.startswith("Vorschlag")


# ------------------------------------------ the Assistant's decide_approval seam


async def _pending_approval(
    db: Any, tenant: uuid.UUID, *, department_id: uuid.UUID, action_type: str = "tool_send"
) -> m.ApprovalRequest:
    approval = m.ApprovalRequest(
        tenant_id=tenant,
        agent_id=uuid.uuid4(),
        department_id=department_id,
        action_type=action_type,
        title="Freigabe erforderlich",
        status="pending",
    )
    db.add(approval)
    await db.flush()
    return approval


async def _decide(
    db: Any,
    tenant: uuid.UUID,
    assistant: m.Agent,
    task: m.Task,
    arguments: dict[str, Any],
    *,
    run_id: uuid.UUID | None = None,
) -> ControlOutcome:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=assistant,
        task=task,
        tc=ToolCall(id="c1", name="decide_approval", arguments=arguments),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
        run_id=run_id,
    )
    assert outcome is not None
    return outcome


@pytest.mark.asyncio
async def test_decide_approval_approves_on_behalf_of_the_human_behind_the_chat(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, all_departments=True)
        approval = await _pending_approval(db, tenant, department_id=task.department_id)

        outcome = await _decide(
            db, tenant, assistant, task, {"approval_id": str(approval.id), "decision": "approve"}
        )
        assert outcome.output == f"Approval {approval.id} approved."
        await db.refresh(approval)
        assert approval.status == "approved"

        # The reject path renders its own past participle rather than a
        # hand-built "reject" + "d" -- "rejectd" would pass a looser check.
        approval2 = await _pending_approval(db, tenant, department_id=task.department_id)
        outcome2 = await _decide(
            db, tenant, assistant, task, {"approval_id": str(approval2.id), "decision": "reject"}
        )
        assert outcome2.output == f"Approval {approval2.id} rejected."
        await db.refresh(approval2)
        assert approval2.status == "rejected"


@pytest.mark.asyncio
async def test_decide_approval_refuses_when_posted_by_someone_other_than_the_session_owner(
    app_session: Any,
) -> None:
    """`_owned_session`'s `copilot:manage` oversight carve-out (chat.py) lets
    an org_admin post into a COLLEAGUE's Assistant session. That admin is not
    the human behind the session -- and a decision made from inside that run
    must not be attributed to, or scoped as, the session's own member just
    because `_member_behind_task` still resolves to them."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        # The session owner, who COULD decide this if it were really them.
        member = await _human_behind(db, tenant, task, all_departments=True)
        approval = await _pending_approval(db, tenant, department_id=task.department_id)
        assert member.subject != "sub-someone-else"

        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=assistant.id,
            task_id=task.id,
            source="chat",
            state="running",
            context={"originating_operator": "sub-someone-else"},
        )
        db.add(run)
        await db.flush()

        outcome = await _decide(
            db,
            tenant,
            assistant,
            task,
            {"approval_id": str(approval.id), "decision": "approve"},
            run_id=run.id,
        )
        assert outcome.output.startswith("ERROR")
        assert "could not resolve who you are acting for" in outcome.output.lower()
        await db.refresh(approval)
        assert approval.status == "pending"


@pytest.mark.asyncio
async def test_decide_approval_refuses_a_foreign_department(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        # A seat in some OTHER department, not the one the approval belongs to.
        other = m.Department(tenant_id=tenant, name="Buchhaltung", frame={})
        db.add(other)
        await db.flush()
        await _human_behind(db, tenant, task, seat_in=other.id)
        approval = await _pending_approval(db, tenant, department_id=task.department_id)

        outcome = await _decide(
            db, tenant, assistant, task, {"approval_id": str(approval.id), "decision": "approve"}
        )
        assert outcome.output.startswith("ERROR")
        assert "not found" in outcome.output.lower()
        await db.refresh(approval)
        assert approval.status == "pending"


@pytest.mark.asyncio
async def test_decide_approval_fails_closed_with_no_chat_session_behind_the_task(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        approval = await _pending_approval(db, tenant, department_id=task.department_id)

        outcome = await _decide(
            db, tenant, assistant, task, {"approval_id": str(approval.id), "decision": "approve"}
        )
        assert outcome.output.startswith("ERROR")
        await db.refresh(approval)
        assert approval.status == "pending"


@pytest.mark.asyncio
async def test_decide_approval_reports_an_already_decided_approval(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, all_departments=True)
        approval = await _pending_approval(db, tenant, department_id=task.department_id)
        approval.status = "approved"
        await db.flush()

        outcome = await _decide(
            db, tenant, assistant, task, {"approval_id": str(approval.id), "decision": "reject"}
        )
        assert outcome.output.startswith("ERROR")
        assert "approved" in outcome.output.lower()


@pytest.mark.asyncio
async def test_decide_approval_refuses_a_management_effect_the_copilot_cannot_apply(
    app_session: Any,
) -> None:
    """hire_agent is a `:manage`-class effect (EFFECT_PERMISSIONS) -- an
    AgentActor (never a HumanActor) can never satisfy `_may_apply_the_effect`
    for it, whatever department it stands in. This is the concrete case for
    NotYourSayAtAll and also the guarantee that the Copilot cannot decide its
    way into a management act."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, all_departments=True)
        approval = await _pending_approval(
            db, tenant, department_id=task.department_id, action_type="hire_agent"
        )

        outcome = await _decide(
            db, tenant, assistant, task, {"approval_id": str(approval.id), "decision": "approve"}
        )
        assert outcome.output.startswith("ERROR")
        await db.refresh(approval)
        assert approval.status == "pending"


@pytest.mark.asyncio
async def test_decide_approval_rejects_an_unknown_option(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, all_departments=True)
        approval = await _pending_approval(
            db, tenant, department_id=task.department_id, action_type="decision"
        )

        outcome = await _decide(
            db,
            tenant,
            assistant,
            task,
            {"approval_id": str(approval.id), "decision": "approve", "option": "not-a-real-option"},
        )
        assert outcome.output.startswith("ERROR")
        await db.refresh(approval)
        assert approval.status == "pending"


@pytest.mark.asyncio
async def test_decide_approval_is_offered_only_to_the_assistant() -> None:
    lead = _agent(is_team_lead=True)
    lead.is_tenant_assistant = False
    assert "decide_approval" not in [
        t.name
        for t in offered_tools(lead, assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    lead.is_tenant_assistant = True
    assert "decide_approval" in [
        t.name
        for t in offered_tools(lead, assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]


@pytest.mark.asyncio
async def test_decide_approval_dispatch_is_refused_for_a_non_assistant_agent(
    app_session: Any,
) -> None:
    """Withholding the tool from the offer list only hides it -- the dispatch
    has to refuse it too, or any agent that guesses the name could decide."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        approval = await _pending_approval(db, tenant, department_id=task.department_id)
        outcome = await _decide(
            db, tenant, agent, task, {"approval_id": str(approval.id), "decision": "approve"}
        )
        assert outcome.output.startswith("ERROR")
        assert "Assistant" in outcome.output


# --------------------------------------------- the 5 status tools' offer gate


@pytest.mark.asyncio
async def test_status_tools_need_assistant_and_the_permission() -> None:
    """Two independent gates, both required: is_tenant_assistant (existing
    precedent, same as decide_approval), and the specific permission in
    copilot_permissions (new to these 5 tools -- unlike decide_approval,
    which has no permission gate of its own)."""
    lead = _agent(is_team_lead=True)
    lead.is_tenant_assistant = False
    names = [
        t.name
        for t in offered_tools(
            lead,
            assigned_skills=[],
            active_skills=[],
            mcp_tools=MCP_TOOLS,
            copilot_permissions=frozenset(
                {"approval:view", "department:view", "agent:view", "budget:view", "statistics:view"}
            ),
        )
    ]
    assert "list_pending_approvals" not in names
    assert "department_status" not in names
    assert "agent_status" not in names
    assert "budget_overview" not in names
    assert "kpi_overview" not in names

    lead.is_tenant_assistant = True
    names = [
        t.name
        for t in offered_tools(
            lead, assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS
        )
    ]
    assert "list_pending_approvals" not in names, (
        "no copilot_permissions given, so nothing is offered"
    )

    names = [
        t.name
        for t in offered_tools(
            lead,
            assigned_skills=[],
            active_skills=[],
            mcp_tools=MCP_TOOLS,
            copilot_permissions=frozenset({"approval:view"}),
        )
    ]
    assert "list_pending_approvals" in names
    assert "department_status" not in names
    assert "agent_status" not in names
    assert "budget_overview" not in names
    assert "kpi_overview" not in names


# ------------------------------------------------------ list_pending_approvals


async def _list_approvals(
    db: Any, tenant: uuid.UUID, assistant: m.Agent, task: m.Task, arguments: dict[str, Any]
) -> ControlOutcome:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=assistant,
        task=task,
        tc=ToolCall(id="c1", name="list_pending_approvals", arguments=arguments),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
    )
    assert outcome is not None
    return outcome


@pytest.mark.asyncio
async def test_list_pending_approvals_returns_only_what_the_human_could_see(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, all_departments=True)
        approval = await _pending_approval(db, tenant, department_id=task.department_id)

        outcome = await _list_approvals(db, tenant, assistant, task, {})
        assert str(approval.id) in outcome.output


@pytest.mark.asyncio
async def test_list_pending_approvals_refuses_without_approval_view(app_session: Any) -> None:
    """Parity with `GET /approvals`'s `require_departmental(APPROVAL_VIEW)`
    403: a member with neither a tenant-wide grant nor a departmental seat
    gets an explicit refusal, not a silently empty list."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task)  # no seat, no all_departments
        await _pending_approval(db, tenant, department_id=task.department_id)

        outcome = await _list_approvals(db, tenant, assistant, task, {})
        assert outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_list_pending_approvals_is_visible_to_a_seat_only_member(
    app_session: Any,
) -> None:
    """The corrected gating formula's whole point: a member with ONLY a
    departmental seat grant (no tenant role at all) still sees their
    department's approvals -- `holds_anywhere` reads the seat directly,
    `authority.tenant_wide` alone would have refused this."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, seat_in=task.department_id)
        approval = await _pending_approval(db, tenant, department_id=task.department_id)

        outcome = await _list_approvals(db, tenant, assistant, task, {})
        assert str(approval.id) in outcome.output


@pytest.mark.asyncio
async def test_list_pending_approvals_dispatch_is_refused_for_a_non_assistant_agent(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        outcome = await _list_approvals(db, tenant, agent, task, {})
        assert outcome.output.startswith("ERROR")


# --------------------------------------------------------- department_status


async def _department_status(
    db: Any, tenant: uuid.UUID, assistant: m.Agent, task: m.Task, arguments: dict[str, Any]
) -> ControlOutcome:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=assistant,
        task=task,
        tc=ToolCall(id="c1", name="department_status", arguments=arguments),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
    )
    assert outcome is not None
    return outcome


@pytest.mark.asyncio
async def test_department_status_lists_only_visible_departments(app_session: Any) -> None:
    """`all_departments` alone does not carry `perm(DEPARTMENT, VIEW)`
    tenant-wide (that flag's only effect is `DepartmentScope.is_unrestricted`,
    which `visible_departments` deliberately does not read -- see that
    module's docstring and `agents.repo.visible_agents`'s identical note).
    A real tenant-wide view needs an assigned role that actually grants it."""
    from oc8.authz.permissions import ORG_ADMIN

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        member = await _human_behind(db, tenant, task)
        member.role_id = (await _builtin_role(db, tenant, ORG_ADMIN)).id
        await db.flush()
        outcome = await _department_status(db, tenant, assistant, task, {})
        assert "Vertrieb" in outcome.output


@pytest.mark.asyncio
async def test_department_status_by_id_404s_for_a_foreign_department(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        other = m.Department(tenant_id=tenant, name="Buchhaltung", frame={})
        db.add(other)
        await db.flush()
        await _human_behind(db, tenant, task, seat_in=task.department_id)
        outcome = await _department_status(
            db, tenant, assistant, task, {"department_id": str(other.id)}
        )
        assert outcome.output.startswith("ERROR")
        assert "not found" in outcome.output.lower()


@pytest.mark.asyncio
async def test_department_status_refuses_without_department_view(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task)
        outcome = await _department_status(db, tenant, assistant, task, {})
        assert outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_department_status_is_visible_to_a_seat_only_member(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, seat_in=task.department_id)
        outcome = await _department_status(db, tenant, assistant, task, {})
        assert "Vertrieb" in outcome.output


@pytest.mark.asyncio
async def test_department_status_dispatch_is_refused_for_a_non_assistant_agent(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        outcome = await _department_status(db, tenant, agent, task, {})
        assert outcome.output.startswith("ERROR")


# -------------------------------------------------------------- agent_status


async def _agent_status(
    db: Any, tenant: uuid.UUID, assistant: m.Agent, task: m.Task, arguments: dict[str, Any]
) -> ControlOutcome:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=assistant,
        task=task,
        tc=ToolCall(id="c1", name="agent_status", arguments=arguments),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
    )
    assert outcome is not None
    return outcome


@pytest.mark.asyncio
async def test_agent_status_lists_only_visible_agents(app_session: Any) -> None:
    from oc8.authz.permissions import ORG_ADMIN

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        # Create a regular agent (not the tenant assistant) in the same department
        other_agent = m.Agent(
            tenant_id=tenant,
            department_id=assistant.department_id,
            name="Nora",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(other_agent)
        await db.flush()
        member = await _human_behind(db, tenant, task)
        member.role_id = (await _builtin_role(db, tenant, ORG_ADMIN)).id
        await db.flush()
        outcome = await _agent_status(db, tenant, assistant, task, {})
        assert "Nora" in outcome.output


@pytest.mark.asyncio
async def test_agent_status_by_id_404s_for_a_foreign_agent(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        other = m.Department(tenant_id=tenant, name="Buchhaltung", frame={})
        db.add(other)
        await db.flush()
        stranger = m.Agent(
            tenant_id=tenant,
            department_id=other.id,
            name="Fremd",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(stranger)
        await db.flush()
        await _human_behind(db, tenant, task, seat_in=task.department_id)
        outcome = await _agent_status(db, tenant, assistant, task, {"agent_id": str(stranger.id)})
        assert outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_agent_status_refuses_without_agent_view(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task)
        outcome = await _agent_status(db, tenant, assistant, task, {})
        assert outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_agent_status_is_visible_to_a_seat_only_member(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        # Create a regular agent (not the tenant assistant) in the same department
        other_agent = m.Agent(
            tenant_id=tenant,
            department_id=assistant.department_id,
            name="Nora",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(other_agent)
        await db.flush()
        await _human_behind(db, tenant, task, seat_in=task.department_id)
        outcome = await _agent_status(db, tenant, assistant, task, {})
        assert "Nora" in outcome.output


@pytest.mark.asyncio
async def test_agent_status_dispatch_is_refused_for_a_non_assistant_agent(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        outcome = await _agent_status(db, tenant, agent, task, {})
        assert outcome.output.startswith("ERROR")


# ------------------------------------------------------------ budget_overview


async def _budget_overview(
    db: Any, tenant: uuid.UUID, assistant: m.Agent, task: m.Task, arguments: dict[str, Any]
) -> ControlOutcome:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=assistant,
        task=task,
        tc=ToolCall(id="c1", name="budget_overview", arguments=arguments),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
    )
    assert outcome is not None
    return outcome


@pytest.mark.asyncio
async def test_budget_overview_reports_the_tenant_wide_budget(app_session: Any) -> None:
    from oc8.authz.permissions import ORG_ADMIN

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        role = await _builtin_role(db, tenant, ORG_ADMIN)
        member = await _human_behind(db, tenant, task)
        member.role_id = role.id
        await db.flush()
        db.add(
            m.Budget(
                tenant_id=tenant, department_id=None, soft_limit_tokens=1000, hard_limit_tokens=2000
            )
        )
        await db.flush()

        outcome = await _budget_overview(db, tenant, assistant, task, {})
        assert "1000" in outcome.output
        assert "2000" in outcome.output


@pytest.mark.asyncio
async def test_budget_overview_refuses_a_department_seat_alone(app_session: Any) -> None:
    """BUDGET_VIEW is not seat-grantable at all -- a seat that grants every
    other status tool must still refuse this one, with no
    scope.holds_anywhere fallback."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, all_departments=True)
        outcome = await _budget_overview(db, tenant, assistant, task, {})
        assert outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_budget_overview_dispatch_is_refused_for_a_non_assistant_agent(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        outcome = await _budget_overview(db, tenant, agent, task, {})
        assert outcome.output.startswith("ERROR")


# ---------------------------------------------------------------- kpi_overview


async def _kpi_overview(
    db: Any, tenant: uuid.UUID, assistant: m.Agent, task: m.Task, arguments: dict[str, Any]
) -> ControlOutcome:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=assistant,
        task=task,
        tc=ToolCall(id="c1", name="kpi_overview", arguments=arguments),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
    )
    assert outcome is not None
    return outcome


@pytest.mark.asyncio
async def test_kpi_overview_for_one_agent(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        # A separate, non-assistant agent: _assistant_and_task's own agent has
        # been flipped to is_tenant_assistant=True, and visible_agent excludes
        # the assistant by design (same as agent_status's Task 5 fixture fix).
        other_agent = m.Agent(
            tenant_id=tenant,
            department_id=assistant.department_id,
            name="Nora",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(other_agent)
        await db.flush()
        await _human_behind(db, tenant, task, seat_in=task.department_id)
        outcome = await _kpi_overview(
            db, tenant, assistant, task, {"agent_id": str(other_agent.id)}
        )
        assert outcome.output.startswith("KPIs for agent Nora")


@pytest.mark.asyncio
async def test_kpi_overview_refuses_both_agent_and_department_at_once(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, all_departments=True)
        outcome = await _kpi_overview(
            db,
            tenant,
            assistant,
            task,
            {"agent_id": str(task.assigned_agent_id), "department_id": str(task.department_id)},
        )
        assert outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_kpi_overview_tenant_wide_refuses_a_department_seat_alone(
    app_session: Any,
) -> None:
    """STATISTICS_VIEW is not seat-grantable, same reasoning as
    budget_overview -- a seat with every other status permission still
    cannot read the tenant-wide figures."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        await _human_behind(db, tenant, task, all_departments=True)
        outcome = await _kpi_overview(db, tenant, assistant, task, {})
        assert outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_kpi_overview_tenant_wide_reports_when_granted(app_session: Any) -> None:
    from oc8.authz.permissions import ORG_ADMIN

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_and_task(db, tenant)
        role = await _builtin_role(db, tenant, ORG_ADMIN)
        member = await _human_behind(db, tenant, task)
        member.role_id = role.id
        await db.flush()
        outcome = await _kpi_overview(db, tenant, assistant, task, {})
        assert outcome.output.startswith("KPIs for the whole tenant")


@pytest.mark.asyncio
async def test_kpi_overview_dispatch_is_refused_for_a_non_assistant_agent(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _dept_agent_task(db, tenant, is_team_lead=True)
        outcome = await _kpi_overview(db, tenant, agent, task, {})
        assert outcome.output.startswith("ERROR")
