"""The builder's three tools: entering the mode, reading what may be chosen,
and proposing -- web door, the session's own member, Agent mode only."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import offered_tools
from oc8.agent.engine import _authorize, run_agent
from oc8.authz.pdp import Effect
from oc8.chat.modes import MODES
from oc8.chat.service import record_assistant_reply
from oc8.copilot.builder_tools import execute_builder_tool
from oc8.copilot.models import CopilotProposal
from oc8.edition.tools import clean_parts
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, Usage, chunk_from_result
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import (
    BuilderOffice,
    builder_office,
    invoice_checker_ops,
    propose_as_admin,
)

COMMON: dict[str, Any] = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}


def _copilot() -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Copilot",
        is_tenant_assistant=True,
        is_team_lead=True,
    )


def _names(tools: list[NeutralTool]) -> set[str]:
    return {t.name for t in tools}


def test_the_copilot_is_offered_the_door_into_agent_mode() -> None:
    names = _names(offered_tools(_copilot(), copilot_door="web", **COMMON))
    assert "enter_agent_mode" in names
    assert not names & {"propose_agent", "builder_options"}


def test_agent_mode_offers_the_builder_and_no_writes() -> None:
    names = _names(
        offered_tools(_copilot(), copilot_door="web", chat_mode=MODES["agent"], **COMMON)
    )
    assert {"builder_options", "propose_agent", "ask_user"} <= names
    assert not names & {"enter_agent_mode", "propose_change", "decide_approval", "memory_write"}


@pytest.mark.parametrize("mode_key", ["plan", "ask", "summarise"])
def test_a_read_only_mode_offers_neither_the_door_nor_the_proposal(mode_key: str) -> None:
    """Ruling A22: a mode may only narrow -- /plan cannot step into Agent mode."""
    names = _names(
        offered_tools(_copilot(), copilot_door="web", chat_mode=MODES[mode_key], **COMMON)
    )
    assert not names & {"enter_agent_mode", "propose_agent", "builder_options"}


@pytest.mark.parametrize("door", ["telegram", "followup", None])
def test_builder_tools_stay_off_every_other_door(door: str | None) -> None:
    for mode in (None, MODES["agent"]):
        names = _names(offered_tools(_copilot(), copilot_door=door, chat_mode=mode, **COMMON))
        assert not names & {"propose_agent", "builder_options", "enter_agent_mode"}, (door, mode)
        if door != "followup":
            # A messenger (or unknown) door cannot answer a park either.
            assert "ask_user" not in names, (door, mode)


def test_an_ordinary_agent_gets_no_builder_tools() -> None:
    plain = m.Agent(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), department_id=uuid.uuid4(), name="Lena"
    )
    names = _names(offered_tools(plain, chat_mode=MODES["agent"], **COMMON))
    assert not names & {"propose_agent", "builder_options", "enter_agent_mode"}


@pytest.mark.parametrize(
    ("tool", "mode_key", "effect"),
    [
        ("propose_agent", "agent", Effect.ALLOW),
        ("builder_options", "agent", Effect.ALLOW),
        ("ask_user", "agent", Effect.ALLOW),
        ("propose_change", "agent", Effect.DENY),
        ("propose_agent", "plan", Effect.DENY),
        ("enter_agent_mode", None, Effect.ALLOW),
        ("enter_agent_mode", "do", Effect.ALLOW),
        ("enter_agent_mode", "plan", Effect.DENY),
        ("enter_agent_mode", "ask", Effect.DENY),
        ("enter_agent_mode", "summarise", Effect.DENY),
        ("enter_agent_mode", "agent", Effect.DENY),
        ("propose_agent", "ask", Effect.DENY),
        ("propose_agent", "summarise", Effect.DENY),
        ("builder_options", "plan", Effect.ALLOW),
    ],
)
def test_the_pep_agrees_with_the_offer(tool: str, mode_key: str | None, effect: Effect) -> None:
    decision = _authorize(
        _copilot(),
        ToolCall(id="c", name=tool, arguments={}),
        frame={"tools": {}},
        tool_policies={},
        connection_key=None,
        tool_scopes=None,
        chat_mode=MODES[mode_key] if mode_key else None,
    )
    assert decision.effect is effect


async def _run(db: Any, office: BuilderOffice, **ctx: Any) -> tuple[m.AgentRun, m.Task]:
    seat = office.admin
    run = m.AgentRun(
        tenant_id=office.tenant,
        agent_id=office.assistant_id,
        source="chat",
        state="running",
        task_id=seat.task_id,
        context={
            "chat_session_id": str(seat.session_id),
            "door": "web",
            "originating_operator": seat.subject,
            "operator_role": "org_admin",
            "task": "Leg mir einen Rechnungsprüfer an",
            **ctx,
        },
    )
    db.add(run)
    await db.flush()
    task = await db.get(m.Task, seat.task_id)
    assert task is not None
    return run, task


async def _call(
    db: Any, office: BuilderOffice, run: m.AgentRun, task: m.Task, name: str, **args: Any
) -> Any:
    agent = await db.get(m.Agent, office.assistant_id)
    return await execute_builder_tool(
        db,
        tenant_id=office.tenant,
        agent=agent,
        task=task,
        tc=ToolCall(id="t", name=name, arguments=args),
        run_id=run.id,
    )


@pytest.mark.asyncio
async def test_enter_agent_mode_sticks_and_lists_the_options(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office)
        outcome = await _call(db, office, run, task, "enter_agent_mode")
        assert outcome is not None and not outcome.output.startswith("ERROR")
        assert "Buchhaltung" in outcome.output and str(office.model_id) in outcome.output
        assert str(office.disabled_model_id) not in outcome.output
        assert str(office.skill_version_id) in outcome.output
        assert str(office.old_skill_version_id) not in outcome.output
        assert '<builder_options nonce="' in outcome.output, "tenant data is fenced"
        assert "odoo" in outcome.output
        session = await db.get(m.ChatSession, office.admin.session_id)
        assert session is not None and session.mode == "agent"
        await db.refresh(run)
        assert run.context["chat_mode"] == "agent"
        assert "[Mode: agent]" in run.context["task"]


@pytest.mark.asyncio
async def test_builder_options_lists_only_connected_systems(
    app_session: AppSessionFactory,
) -> None:
    """Ruling A12: a frame-enabled system with no connected connection is not
    offered -- apply would refuse it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        conn = (
            await db.execute(select(m.McpConnection).where(m.McpConnection.tenant_id == tenant))
        ).scalar_one()
        conn.connected = False
        await db.flush()
        run, task = await _run(db, office, chat_mode="agent")
        outcome = await _call(db, office, run, task, "builder_options")
        assert outcome is not None and not outcome.output.startswith("ERROR")
        assert "odoo" not in outcome.output


@pytest.mark.asyncio
async def test_enter_agent_mode_is_refused_under_plan(app_session: AppSessionFactory) -> None:
    """The dispatch refuses it too, not only the PEP (Ruling A22)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office, chat_mode="plan")
        outcome = await _call(db, office, run, task, "enter_agent_mode")
        assert outcome is not None and outcome.output.startswith("ERROR")
        session = await db.get(m.ChatSession, office.admin.session_id)
        assert session is not None and session.mode is None
        await db.refresh(run)
        assert run.context["chat_mode"] == "plan"


@pytest.mark.asyncio
async def test_enter_agent_mode_is_refused_on_a_messenger_turn(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office, door="telegram")
        outcome = await _call(db, office, run, task, "enter_agent_mode")
        assert outcome is not None and outcome.output.startswith("ERROR")
        session = await db.get(m.ChatSession, office.admin.session_id)
        assert session is not None and session.mode is None, "A7: never set off the web"


@pytest.mark.asyncio
async def test_propose_agent_needs_agent_mode(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office)
        outcome = await _call(
            db, office, run, task, "propose_agent", operations=invoice_checker_ops(office)
        )
        assert outcome is not None and outcome.output.startswith("ERROR")
        assert (await db.execute(select(CopilotProposal))).scalars().all() == []


@pytest.mark.asyncio
async def test_propose_agent_puts_a_card_on_the_answer(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office, chat_mode="agent")
        outcome = await _call(
            db, office, run, task, "propose_agent", operations=invoice_checker_ops(office)
        )
        assert outcome is not None and not outcome.output.startswith("ERROR"), outcome.output
        proposal = (await db.execute(select(CopilotProposal))).scalar_one()
        assert proposal.kind == "agent_builder" and proposal.created_by == "admin"
        await db.refresh(run)
        assert run.context["builder_proposal_id"] == str(proposal.id)

        run.state = "done"
        await record_assistant_reply(db, run=run, output="Hier ist dein Rechnungsprüfer.")
        reply = (
            await db.execute(select(m.ChatMessage).where(m.ChatMessage.role == "assistant"))
        ).scalar_one()
        assert reply.parts == [{"type": "agent_builder", "proposalId": str(proposal.id)}]
        await db.refresh(run)
        assert run.context.get("builder_proposal_id") is None, "consumed"


@pytest.mark.asyncio
async def test_a_second_proposal_replaces_the_first_on_the_card(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office, chat_mode="agent")
        ops = invoice_checker_ops(office)
        first = await _call(db, office, run, task, "propose_agent", operations=ops)
        second = await _call(db, office, run, task, "propose_agent", operations=ops)
        assert not first.output.startswith("ERROR") and not second.output.startswith("ERROR")
        rows = (await db.execute(select(CopilotProposal))).scalars().all()
        assert sorted(r.status for r in rows) == ["draft", "superseded"]
        (draft,) = [r for r in rows if r.status == "draft"]
        await db.refresh(run)
        assert run.context["builder_proposal_id"] == str(draft.id)


@pytest.mark.asyncio
async def test_a_refused_proposal_tells_the_model_why_without_values(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office, chat_mode="agent")
        ops = invoice_checker_ops(office)
        ops[1]["modelConfigId"] = str(office.disabled_model_id)
        outcome = await _call(db, office, run, task, "propose_agent", operations=ops)
        assert outcome is not None and outcome.output.startswith("ERROR")
        assert "model_not_allowed" in outcome.output and "Rechnungsprüfer" not in outcome.output
        assert (await db.execute(select(CopilotProposal))).scalars().all() == []
        await db.refresh(run)
        assert "builder_proposal_id" not in run.context


@pytest.mark.asyncio
async def test_a_concurrent_proposal_asks_the_model_to_retry(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 4: losing the open-proposal race is not the model's fault -- the
    attempt is rolled back and the model is told to call again."""
    from oc8.copilot import builder_tools
    from oc8.copilot.builder_ops import BuilderInvalid

    real = builder_tools.create_builder_proposal

    async def _lost(*a: Any, **k: Any) -> Any:
        # Everything the real call does -- the prior draft superseded, the new
        # one stored -- and then the race lost at the unique index.
        await real(*a, **k)
        raise BuilderInvalid("superseded_concurrently")

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        prior = await propose_as_admin(db, office)
        run, task = await _run(db, office, chat_mode="agent")
        monkeypatch.setattr(builder_tools, "create_builder_proposal", _lost)
        outcome = await _call(
            db,
            office,
            run,
            task,
            "propose_agent",
            operations=invoice_checker_ops(office, name="Mahnwesen"),
        )
        assert outcome is not None and outcome.output.startswith("ERROR")
        assert "again" in outcome.output and "superseded_concurrently" not in outcome.output
        rows = (await db.execute(select(CopilotProposal))).scalars().all()
        assert [r.id for r in rows] == [prior.id], "the lost attempt left nothing"
        await db.refresh(prior)
        assert prior.status == "draft" and prior.revision == 1, "the prior draft is restored"
        await db.refresh(run)
        assert "builder_proposal_id" not in run.context


@pytest.mark.asyncio
async def test_awaiting_approval_tells_the_model_to_wait(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        pending = await propose_as_admin(db, office)
        pending.status = "awaiting_approval"
        await db.flush()
        run, task = await _run(db, office, chat_mode="agent")
        outcome = await _call(
            db, office, run, task, "propose_agent", operations=invoice_checker_ops(office)
        )
        assert outcome is not None and outcome.output.startswith("ERROR")
        assert "awaiting_approval" in outcome.output and "wait" in outcome.output


@pytest.mark.asyncio
async def test_propose_agent_named_on_a_messenger_turn_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office, chat_mode="agent", door="telegram")
        outcome = await _call(
            db, office, run, task, "propose_agent", operations=invoice_checker_ops(office)
        )
        assert outcome is not None and outcome.output.startswith("ERROR")
        assert (await db.execute(select(CopilotProposal))).scalars().all() == []


@pytest.mark.asyncio
async def test_only_the_sessions_own_member_builds(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office, chat_mode="agent", originating_operator="lead")
        outcome = await _call(
            db, office, run, task, "propose_agent", operations=invoice_checker_ops(office)
        )
        assert outcome is not None and outcome.output.startswith("ERROR")


@pytest.mark.asyncio
async def test_an_ordinary_agent_cannot_call_the_builder(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, task = await _run(db, office, chat_mode="agent")
        plain = m.Agent(tenant_id=tenant, department_id=office.department_id, name="Lena")
        db.add(plain)
        await db.flush()
        outcome = await execute_builder_tool(
            db,
            tenant_id=tenant,
            agent=plain,
            task=task,
            tc=ToolCall(id="t", name="builder_options", arguments={}),
            run_id=run.id,
        )
        assert outcome is not None and outcome.output.startswith("ERROR")


def test_an_edition_part_cannot_pose_as_the_builder_card() -> None:
    assert clean_parts([{"type": "agent_builder", "proposalId": "x"}]) == []


class _EntersThenProposesAChange:
    """One step: enter_agent_mode and propose_change side by side; then stop."""

    def __init__(self) -> None:
        self.calls = 0
        self.offered: list[set[str]] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        self.offered.append({t.name for t in req.tools or []})
        if self.calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[
                    ToolCall(id="c1", name="enter_agent_mode", arguments={}),
                    ToolCall(
                        id="c2",
                        name="propose_change",
                        arguments={
                            "operation_type": "department.create",
                            "payload": {"name": "Einkauf"},
                        },
                    ),
                ],
                usage=Usage(1, 1),
                stop_reason="tool_use",
                provider="ollama",
                model="stub",
            )
        return CompletionResult(
            text="fertig",
            tool_calls=[],
            usage=Usage(1, 1),
            stop_reason="stop",
            provider="ollama",
            model="stub",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


@pytest.mark.asyncio
async def test_writes_are_withheld_for_the_rest_of_the_leg_that_entered_agent_mode(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling A6: once enter_agent_mode succeeds, the engine runs the rest of
    the leg in Agent mode -- the next call in the same step is decided by it,
    and the next completion is offered the builder, not the writes."""
    router = _EntersThenProposesAChange()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        run, _task = await _run(db, office)
        assistant = await db.get(m.Agent, office.assistant_id)
        assert assistant is not None
        assistant.model_config_id = office.model_id
        await db.flush()
        result = await run_agent(
            db,
            agent=assistant,
            task_text="Leg mir einen Rechnungsprüfer an",
            tenant_id=tenant,
            run_id=run.id,
            originating_operator=office.admin.subject,
        )
        proposals = (await db.execute(select(CopilotProposal))).scalars().all()
        audited = await db.execute(
            select(m.AuditEvent.action, m.AuditEvent.decision).where(
                m.AuditEvent.category == "tool_action"
            )
        )
        decisions = {action: decision for action, decision in audited.all()}
    assert decisions["tool.call:propose_change"] == "deny"
    by_tool = {c["tool"]: c for c in result.tool_calls}
    assert by_tool["enter_agent_mode"]["state"] == "done"
    # Refused by the dispatch on the PEP's DENY: the engine records a
    # dispatched control tool's ERROR as "failed" (the audit above says deny).
    assert by_tool["propose_change"]["state"] == "failed"
    assert proposals == []
    assert len(router.offered) >= 2
    assert "propose_change" in router.offered[0] and "enter_agent_mode" in router.offered[0]
    assert {"propose_agent", "builder_options"} <= router.offered[1]
    assert not router.offered[1] & {"propose_change", "enter_agent_mode"}


@pytest.mark.parametrize(
    ("routed", "scopes", "mode_key"),
    [
        (False, {"modify": ["propose_agent"]}, "agent"),
        (True, {"modify": ["propose_agent"]}, "agent"),
        (True, {"modify": ["propose_agent"]}, None),
        (True, None, "agent"),
        (True, None, None),
    ],
)
def test_a_connection_tool_named_like_a_builder_tool_gets_no_core_pass(
    routed: bool, scopes: dict[str, Any] | None, mode_key: str | None
) -> None:
    """Ruling A8 at the PEP: the core propose_agent is exempt in Agent mode and
    allowed early; a connection's own tool of that name -- routed to it
    (`odoo__propose_agent`) or listed in its scopes -- gets neither and is
    decided by the frame, which grants nothing here."""
    decision = _authorize(
        _copilot(),
        ToolCall(id="c", name="propose_agent", arguments={}),
        frame={"tools": {}},
        tool_policies={},
        connection_key="odoo",
        tool_scopes=scopes,
        chat_mode=MODES[mode_key] if mode_key else None,
        routed=routed,
    )
    assert decision.effect is Effect.DENY
