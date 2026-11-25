"""The transcript preamble both runtimes must put in front of the model.

This exists because the isolated runtime used to seed only the system prompt,
leaving an agent under isolation blind to its memory, its knowledge base, its
department roster and its skills -- which in turn made delegate_task unusable
(no valid agent_id to name) and memory_write pointless (nothing ever read it
back).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.preamble import build_run_preamble, system_prompt
from oc8.authz.permissions import AGENT, APPROVAL, VIEW, perm
from oc8.chat.replay import ChatReplay
from oc8.modelrouter.types import ImagePart
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

DEF: dict[str, Any] = {
    "oc8_skill": 1,
    "id": "sk-invoice-check",
    "version": "1.0.0",
    "instruction": "Match invoices against purchase orders.",
    "requires": {"tools": [{"tool": "odoo", "rights": ["read", "write"]}], "kbs": []},
    "guardrails": [],
}


async def _lead_with_mate_and_skill(
    db: Any, tenant: uuid.UUID
) -> tuple[m.Agent, m.Agent]:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    lead = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Nora", role_title="Lead",
        mission="Sell things.", status="idle", definition={}, presentation={},
        is_team_lead=True,
    )
    mate = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Rico", role_title="Rep",
        status="idle", definition={}, presentation={},
    )
    skill = m.Skill(
        tenant_id=tenant, name="Invoice Review",
        description="Validates invoices against POs.", author="oc8 core",
    )
    db.add_all([lead, mate, skill])
    await db.flush()
    version = m.SkillVersion(
        tenant_id=tenant, skill_id=skill.id, semver="1.0.0",
        definition=DEF, artifact_hash=b"\x00" * 32,
    )
    db.add(version)
    await db.flush()
    skill.current_version_id = version.id
    db.add(
        m.SkillAssignment(
            tenant_id=tenant, agent_id=lead.id, skill_version_id=version.id, enabled=True
        )
    )
    await db.flush()
    return lead, mate


async def test_a_team_lead_preamble_carries_roster_and_skill_catalog(
    app_session: AppSessionFactory,
) -> None:
    """A team lead can only name a real agent_id if it is told its colleagues,
    and can only invoke a skill it is told about."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, mate = await _lead_with_mate_and_skill(db, tenant)

        pre = await build_run_preamble(
            db, agent=lead, tenant_id=tenant, task_text="Erstelle ein Angebot",
            frame={}, model_locality="eu",
            caps=ModelCaps(),
            max_steps=40,
        )

    joined = "\n".join(msg.content for msg in pre.messages)
    assert str(mate.id) in joined, "the roster must name the mate's real id"
    assert "Rico" in joined
    assert "Invoice Review" in joined, "the skills catalog must be seeded"
    assert pre.skill_tool_names, "skill tool names drive the frame bypass in _authorize"

    # The task the agent was given is the LAST message; the A2 run-context
    # block is the one immediately before it (also a user turn, by design --
    # see run_context_block) -- everything earlier is context the core
    # supplies as system messages.
    assert pre.messages[-1].role == "user"
    assert pre.messages[-1].content == "Erstelle ein Angebot"
    assert pre.messages[-2].role == "user"
    assert pre.messages[-2].content.startswith("# Run context\n")
    assert [msg.role for msg in pre.messages[:-2]] == ["system"] * (len(pre.messages) - 2)
    assert pre.messages[0].content.startswith("You are Nora")


async def test_a_plain_agent_gets_no_roster_and_no_delegation_context(
    app_session: AppSessionFactory,
) -> None:
    """Only a team lead delegates, so only a team lead is told the roster."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        solo = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Solo", status="idle",
            definition={}, presentation={},
        )
        mate = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Nachbar", status="idle",
            definition={}, presentation={},
        )
        db.add_all([solo, mate])
        await db.flush()

        pre = await build_run_preamble(
            db, agent=solo, tenant_id=tenant, task_text="mach was",
            frame={}, model_locality="eu",
            caps=ModelCaps(),
            max_steps=40,
        )

    joined = "\n".join(msg.content for msg in pre.messages)
    assert str(mate.id) not in joined
    assert pre.assigned_skills == []
    assert pre.skill_tool_names == frozenset()
    assert pre.has_knowledge is False


async def test_the_tenant_assistant_gets_every_departments_agents_not_just_its_own(
    app_session: AppSessionFactory,
) -> None:
    """The Assistant sits alone in its own single-agent department
    (get_or_create_assistant), so a department-scoped roster is always empty
    for it -- yet its delegation guard lets it reach any department the
    acting human can. Observed live: offered delegate_task with no roster at
    all, it never once named a real agent_id, and instead asked the human
    whether their tenant had anyone for the job, on every turn."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant_dept = m.Department(tenant_id=tenant, name="oc8 Assistant", frame={})
        helpdesk = m.Department(tenant_id=tenant, name="Helpdesk", frame={})
        db.add_all([assistant_dept, helpdesk])
        await db.flush()
        assistant = m.Agent(
            tenant_id=tenant, department_id=assistant_dept.id, name="oc8 Assistant",
            status="idle", definition={}, presentation={},
            is_team_lead=True, is_tenant_assistant=True,
        )
        lennart = m.Agent(
            tenant_id=tenant, department_id=helpdesk.id, name="Lennart",
            role_title="First Level IT Support", status="idle",
            definition={}, presentation={},
        )
        db.add_all([assistant, lennart])
        await db.flush()

        pre = await build_run_preamble(
            db, agent=assistant, tenant_id=tenant, task_text="Wie viele Tickets sind offen?",
            frame={}, model_locality="eu",
            caps=ModelCaps(),
            max_steps=40,
        )

    joined = "\n".join(msg.content for msg in pre.messages)
    assert str(lennart.id) in joined, "the tenant-wide roster must name a real agent_id"
    assert "Lennart" in joined
    assert "Helpdesk" in joined, "the department a candidate belongs to should be legible too"


async def test_the_tenant_assistant_is_excluded_from_its_own_roster(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="oc8 Assistant", frame={})
        db.add(dept)
        await db.flush()
        assistant = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="oc8 Assistant",
            status="idle", definition={}, presentation={},
            is_team_lead=True, is_tenant_assistant=True,
        )
        db.add(assistant)
        await db.flush()

        pre = await build_run_preamble(
            db, agent=assistant, tenant_id=tenant, task_text="Hallo",
            frame={}, model_locality="eu",
            caps=ModelCaps(),
            max_steps=40,
        )

    joined = "\n".join(msg.content for msg in pre.messages)
    assert str(assistant.id) not in joined, "the Assistant is not a delegation target for itself"


ON: dict[str, Any] = {"enabled": True, "read": True, "modify": True}
OFF: dict[str, Any] = {"enabled": False, "read": False, "modify": False}


async def _copilot_with_person(
    db: Any, tenant: uuid.UUID, *, seats: list[m.Department], door: dict[str, Any] | None = None
) -> tuple[m.Agent, m.Task, m.AgentRun]:
    """The Copilot on a live web turn its own member wrote (`on_behalf_of`),
    for a person whose role holds `run:start` -- reaching the departments they
    hold a seat in, not every agent (no tenant-wide `agent:view`)."""
    from oc8.authz.permissions import COPILOT_USE, RUN_START

    assistant, task = await _assistant_dept_task(db, tenant)
    role = m.Role(tenant_id=tenant, name=f"r-{uuid.uuid4().hex[:8]}", kind="human")
    db.add(role)
    await db.flush()
    for permission in (COPILOT_USE, RUN_START):
        db.add(m.RolePermission(tenant_id=tenant, role_id=role.id, permission=permission))
    member = m.OrgMember(
        tenant_id=tenant, subject=f"anna-{uuid.uuid4()}", subject_uuid=uuid.uuid4(),
        role_id=role.id,
    )
    db.add(member)
    await db.flush()
    for dept in seats:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant, member_id=member.id, department_id=dept.id,
                seat_role="dept_viewer",
            )
        )
    session = m.ChatSession(
        tenant_id=tenant, agent_id=assistant.id, member_id=member.id, task_id=task.id
    )
    db.add(session)
    await db.flush()
    context: dict[str, Any] = (
        {"chat_session_id": str(session.id), **door}
        if door is not None
        else {"door": "web", "on_behalf_of": str(member.id), "chat_session_id": str(session.id)}
    )
    run = m.AgentRun(
        tenant_id=tenant, agent_id=assistant.id, task_id=task.id, source="chat",
        state="running", context=context,
    )
    db.add(run)
    await db.flush()
    return assistant, task, run


async def _roster_of(db: Any, tenant: uuid.UUID, assistant: m.Agent, **kw: Any) -> str:
    pre = await build_run_preamble(
        db, agent=assistant, tenant_id=tenant, task_text="hi", frame={},
        model_locality="cloud", caps=ModelCaps(), max_steps=40, **kw,
    )
    return "\n".join(msg.content for msg in pre.messages if isinstance(msg.content, str))


async def test_the_copilot_roster_names_each_agents_tools(
    app_session: AppSessionFactory,
) -> None:
    """Each line says which tools the agent has -- enabled in its department's
    frame and not switched off by its own narrowing; keys, sorted, at most 8."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, _task = await _assistant_dept_task(db, tenant)
        helpdesk = m.Department(
            tenant_id=tenant, name="Helpdesk",
            frame={"tools": {"things": ON, "mail": ON, "ledger": OFF, "files": ON}},
        )
        wide = m.Department(
            tenant_id=tenant, name="Zentrale",
            frame={"tools": {f"t{i:02d}": ON for i in range(10)}},
        )
        db.add_all([helpdesk, wide])
        await db.flush()
        lennart = m.Agent(
            tenant_id=tenant, department_id=helpdesk.id, name="Lennart",
            role_title="First Level IT Support", status="idle", definition={},
            presentation={}, narrowing={"tools": {"files": OFF}},
        )
        bare = m.Agent(
            tenant_id=tenant, department_id=wide.id, name="Wim", status="idle",
            definition={}, presentation={},
        )
        db.add_all([lennart, bare])
        await db.flush()

        joined = await _roster_of(db, tenant, assistant)

    assert (
        f"- {lennart.id}: Lennart (Helpdesk, First Level IT Support) — tools: mail, things"
        in joined
    )
    eight = ", ".join(f"t{i:02d}" for i in range(8))
    assert f"- {bare.id}: Wim (Zentrale) — tools: {eight}, …" in joined


@pytest.mark.parametrize(
    "door",
    [
        None,
        {"door": "followup", "followup": {"id": "f1"}},
        {"door": "telegram", "chat_channel": "telegram", "chat_channel_external_id": "42"},
    ],
    ids=["web", "followup", "telegram"],
)
async def test_the_copilot_roster_hides_agents_the_person_may_not_run(
    app_session: AppSessionFactory, door: dict[str, Any] | None
) -> None:
    """On every turn that acts for a person, only the agents that person may
    start are named -- what `_delegate` lets through: the Copilot itself
    crosses departments, the person may not."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        helpdesk = m.Department(tenant_id=tenant, name="Helpdesk", frame={})
        ledger = m.Department(tenant_id=tenant, name="Buchhaltung", frame={})
        db.add_all([helpdesk, ledger])
        await db.flush()
        assistant, task, run = await _copilot_with_person(
            db, tenant, seats=[helpdesk], door=door
        )
        lennart = m.Agent(
            tenant_id=tenant, department_id=helpdesk.id, name="Lennart", status="idle",
            definition={}, presentation={},
        )
        fremd = m.Agent(
            tenant_id=tenant, department_id=ledger.id, name="Fremd", status="idle",
            definition={}, presentation={},
        )
        db.add_all([lennart, fremd])
        await db.flush()

        joined = await _roster_of(db, tenant, assistant, task=task, run_id=run.id)

    assert f"- {lennart.id}: Lennart (Helpdesk)" in joined
    assert str(fremd.id) not in joined
    assert "Fremd" not in joined


async def test_the_roster_of_a_wake_up_uses_the_role_of_the_persons_chat_run(
    app_session: AppSessionFactory,
) -> None:
    """No assigned role: the Copilot's wake-up after a delegated result names
    the agents the role claim on the person's own web turn -- its causal
    parent -- lets them run: the claim `_delegate` judges with, not an empty
    roster."""
    from oc8.authz.permissions import OPERATOR

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        helpdesk = m.Department(tenant_id=tenant, name="Helpdesk", frame={})
        db.add(helpdesk)
        await db.flush()
        assistant, task, wake = await _copilot_with_person(
            db,
            tenant,
            seats=[helpdesk],
            door={"door": "web", "parent_task_id": "", "delegation_depth": 1},
        )
        session = await db.scalar(select(m.ChatSession).where(m.ChatSession.task_id == task.id))
        member = await db.get(m.OrgMember, session.member_id)
        member.role_id = None
        web = m.AgentRun(
            tenant_id=tenant, agent_id=assistant.id, task_id=task.id, source="chat",
            state="done",
            context={
                "door": "web", "chat_session_id": str(session.id),
                "originating_operator": member.subject, "operator_role": OPERATOR,
            },
        )
        db.add(web)
        await db.flush()
        # The causal chain the executor records: the sub-run the web turn
        # delegated, and the wake-up its result caused.
        sub = m.AgentRun(
            tenant_id=tenant, agent_id=assistant.id, source="delegation", state="done",
            context={"parent_task_id": str(task.id), "parent_run_id": str(web.id)},
        )
        db.add(sub)
        await db.flush()
        wake.context = {
            **wake.context,
            "parent_task_id": str(task.id),
            "originating_operator": member.subject,
            "wake_of_run_id": str(sub.id),
        }
        lennart = m.Agent(
            tenant_id=tenant, department_id=helpdesk.id, name="Lennart", status="idle",
            definition={}, presentation={},
        )
        db.add(lennart)
        await db.flush()

        joined = await _roster_of(db, tenant, assistant, task=task, run_id=wake.id)

    assert f"- {lennart.id}: Lennart (Helpdesk)" in joined


async def test_has_knowledge_is_true_once_a_kb_is_granted_to_the_department(
    app_session: AppSessionFactory,
) -> None:
    """has_knowledge gates whether offered_tools() offers search_knowledge at
    all (see oc8.agent.control_tools) -- independent of whether THIS task's
    initial text happens to retrieve anything: a scheduled agent's task text
    is only "check the inbox", so the real query only becomes clear once it
    has read the ticket, which is what the tool is for."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="IT-Support", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Lennart", status="idle",
            definition={}, presentation={},
        )
        kb = m.KnowledgeBase(tenant_id=tenant, name="OPaaS", embedding_model="bge-large")
        db.add_all([agent, kb])
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="department", grantee_id=dept.id
            )
        )
        await db.flush()

        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="Jetzt ausführen",
            frame={}, model_locality="cloud",
            caps=ModelCaps(),
            max_steps=40,
        )

    assert pre.has_knowledge is True


async def test_has_instruction_files_is_false_with_no_attachment(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Solo", status="idle",
            definition={}, presentation={},
        )
        db.add(agent)
        await db.flush()

        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="mach was",
            frame={}, model_locality="eu",
            caps=ModelCaps(),
            max_steps=40,
        )

    assert pre.has_instruction_files is False
    joined = "\n".join(msg.content for msg in pre.messages if isinstance(msg.content, str))
    assert "read_instruction_file" not in joined


async def test_has_instruction_files_is_true_once_one_is_attached(
    app_session: AppSessionFactory,
) -> None:
    """Gates whether offered_tools() offers read_instruction_file at all (see
    oc8.agent.control_tools) -- and, per the design doc's Instructions
    Attachment Flow §3, the Instructions text itself gets one line
    auto-appended so the agent knows the tool exists, without the file's
    actual content ever entering the standing prompt."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Solo", status="idle",
            definition={}, presentation={},
        )
        db.add(agent)
        await db.flush()
        db.add(
            m.FileAttachment(
                tenant_id=tenant,
                owner_type="agent_instructions",
                owner_id=agent.id,
                bucket_key="policy-attached",
                filename="policy.pdf",
                content_type="application/pdf",
                size_bytes=10,
                extracted_text="Refunds within 30 days.",
                is_image=False,
            )
        )
        await db.flush()

        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="mach was",
            frame={}, model_locality="eu",
            caps=ModelCaps(),
            max_steps=40,
        )

    assert pre.has_instruction_files is True
    joined = "\n".join(msg.content for msg in pre.messages if isinstance(msg.content, str))
    assert "read_instruction_file" in joined
    # The note nudges the agent toward the tool -- it must never carry the
    # attachment's own content, or read_instruction_file's whole point (never
    # auto-injecting file content into every run) is defeated.
    assert "Refunds within 30 days." not in joined


async def test_the_provenance_rule_precedes_anything_a_stranger_wrote(
    app_session: AppSessionFactory,
) -> None:
    """Said once, before any external content can arrive. A ticket body is
    written by someone outside the company and lands in the same context as
    oc8's own mission; without this it arrives in the same voice."""
    from oc8.agent.provenance import RULE

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Kundenservice", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Sina", status="idle",
            definition={}, presentation={},
        )
        db.add(agent)
        await db.flush()
        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="Bearbeite ein Ticket",
            frame={}, model_locality="cloud",
            caps=ModelCaps(),
            max_steps=40,
        )
    systems = [msg.content for msg in pre.messages if msg.role == "system"]
    assert RULE in systems


async def _solo_agent(db: Any, tenant: uuid.UUID) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Nora", status="idle",
        definition={}, presentation={},
    )
    db.add(agent)
    await db.flush()
    return agent


async def test_preamble_appends_image_content_when_supported(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _solo_agent(db, tenant)

        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="What's in this?",
            frame={}, model_locality="cloud",
            task_images=[ImagePart(data=b"fake-png-bytes", content_type="image/png")],
            supports_vision=True,
            caps=ModelCaps(),
            max_steps=40,
        )

    last = pre.messages[-1]
    assert last.role == "user"
    assert isinstance(last.content, list)
    assert any(isinstance(p, ImagePart) for p in last.content)


async def test_preamble_falls_back_to_a_text_note_when_vision_unsupported(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _solo_agent(db, tenant)

        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="What's in this?",
            frame={}, model_locality="cloud",
            task_images=[ImagePart(data=b"fake-png-bytes", content_type="image/png")],
            supports_vision=False,
            caps=ModelCaps(),
            max_steps=40,
        )

    last = pre.messages[-1]
    assert isinstance(last.content, str)
    assert "cannot process images" in last.content


async def _assistant_dept_task(db, tenant: uuid.UUID) -> tuple[m.Agent, m.Task]:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    assistant = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Assistant",
        status="running",
        definition={},
        presentation={},
        is_team_lead=True,
        is_tenant_assistant=True,
    )
    db.add(assistant)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=assistant.id,
        title="Chat",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    return assistant, task


async def test_build_run_preamble_grants_no_copilot_permissions_without_a_task(
    app_session: AppSessionFactory,
) -> None:
    """delegated/scheduled runs (task=None) get nothing, same fail-closed
    direction as _resolve_agent_actor itself."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, _task = await _assistant_dept_task(db, tenant)
        preamble = await build_run_preamble(
            db,
            agent=assistant,
            tenant_id=tenant,
            task_text="hi",
            frame={},
            model_locality="cloud",
            caps=ModelCaps(),
            max_steps=40,
        )
    assert preamble.copilot_permissions == frozenset()


async def test_build_run_preamble_grants_permissions_the_human_behind_the_chat_holds(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task = await _assistant_dept_task(db, tenant)
        member = m.OrgMember(
            tenant_id=tenant,
            subject=f"anna-{uuid.uuid4()}",
            subject_uuid=uuid.uuid4(),
            all_departments=True,
        )
        db.add(member)
        await db.flush()
        db.add(
            m.ChatSession(
                tenant_id=tenant,
                agent_id=assistant.id,
                member_id=member.id,
                task_id=task.id,
            )
        )
        await db.flush()

        preamble = await build_run_preamble(
            db,
            agent=assistant,
            tenant_id=tenant,
            task_text="hi",
            frame={},
            model_locality="cloud",
            caps=ModelCaps(),
            max_steps=40,
            task=task,
        )
    assert perm(APPROVAL, VIEW) in preamble.copilot_permissions
    assert perm(AGENT, VIEW) in preamble.copilot_permissions
    joined = "\n".join(msg.content for msg in preamble.messages if isinstance(msg.content, str))
    # member has no display_name set, so the run-context "Acting for" line
    # falls back to subject -- also proves _gated_copilot_permissions reused
    # this same resolved actor rather than re-resolving it a second time.
    assert f"- Acting for: {member.subject}" in joined


def test_system_prompt_has_no_status_opener_for_the_assistant() -> None:
    assistant = m.Agent(
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Assistant",
        status="running",
        definition={},
        presentation={},
        is_tenant_assistant=True,
    )
    ordinary = m.Agent(
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        status="running",
        definition={},
        presentation={},
        is_tenant_assistant=False,
    )
    for agent in (assistant, ordinary):
        prompt = system_prompt(agent, caps=ModelCaps(), tenant_name="Acme")
        assert "At the start of a new conversation" not in prompt
        assert "list_pending_approvals" not in prompt


@pytest.mark.parametrize(
    "source,expected",
    [
        ("chat", "chat"),
        ("cron", "schedule"),
        ("decision", "decision follow-up"),
        ("webhook", "webhook trigger"),
        ("event", "trigger"),
        ("manual", "manual run"),
        ("handoff", "handoff"),
    ],
)
async def test_origin_label_covers_every_non_delegation_run_source(
    app_session: AppSessionFactory, source: str, expected: str
) -> None:
    """A2's "Origin" line, driven end to end through a real AgentRun row for
    each value the ck_agent_run_source CHECK constraint allows besides
    "delegation" (covered separately below, since it needs a parent task)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _solo_agent(db, tenant)
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", source=source)
        db.add(run)
        await db.flush()
        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="hi",
            frame={}, model_locality="cloud", caps=ModelCaps(), max_steps=40,
            run_id=run.id,
        )
    joined = "\n".join(msg.content for msg in pre.messages if isinstance(msg.content, str))
    assert f"- Origin: {expected}" in joined


async def test_origin_label_names_the_delegating_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        parent_agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Nora", status="idle",
            definition={}, presentation={},
        )
        child_agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Rico", status="idle",
            definition={}, presentation={},
        )
        db.add_all([parent_agent, child_agent])
        await db.flush()
        parent_task = m.Task(
            tenant_id=tenant, department_id=dept.id, assigned_agent_id=parent_agent.id,
            title="Parent", state="in_progress",
        )
        db.add(parent_task)
        await db.flush()
        child_task = m.Task(
            tenant_id=tenant, department_id=dept.id, assigned_agent_id=child_agent.id,
            parent_task_id=parent_task.id, title="Child", state="in_progress",
        )
        db.add(child_task)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant, agent_id=child_agent.id, task_id=child_task.id,
            state="running", source="delegation",
        )
        db.add(run)
        await db.flush()
        pre = await build_run_preamble(
            db, agent=child_agent, tenant_id=tenant, task_text="hi",
            frame={}, model_locality="cloud", caps=ModelCaps(), max_steps=40,
            run_id=run.id,
        )
    joined = "\n".join(msg.content for msg in pre.messages if isinstance(msg.content, str))
    assert "- Origin: delegation from Nora" in joined


async def test_origin_label_falls_back_when_the_parent_agent_cannot_be_resolved(
    app_session: AppSessionFactory,
) -> None:
    """A delegated run whose task has no parent (or whose parent has no
    assigned agent) degrades to the flat "delegation" label instead of
    raising -- best-effort, not a hard requirement."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _solo_agent(db, tenant)
        task = m.Task(
            tenant_id=tenant, department_id=agent.department_id, assigned_agent_id=agent.id,
            title="Child", state="in_progress",
        )
        db.add(task)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, task_id=task.id,
            state="running", source="delegation",
        )
        db.add(run)
        await db.flush()
        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="hi",
            frame={}, model_locality="cloud", caps=ModelCaps(), max_steps=40,
            run_id=run.id,
        )
    joined = "\n".join(msg.content for msg in pre.messages if isinstance(msg.content, str))
    assert "- Origin: delegation\n" in joined


async def test_systems_you_can_reach_names_only_systems_the_agent_can_use(
    app_session: AppSessionFactory,
) -> None:
    """The run context lists a system only if one of its tools can be offered:
    not one the department switched off, nor one the agent's narrowing did."""
    tenant = uuid.uuid4()
    frame = {
        "tools": {
            "microsoft365": {"enabled": True, "read": True, "modify": True, "only": ["files_list"]},
            "odoo": {"enabled": True, "read": True, "modify": True},
            "jira": {"enabled": False, "read": True, "modify": True},
        }
    }
    async with app_session(tenant) as db:
        agent = await _solo_agent(db, tenant)
        agent.narrowing = {"tools": {"odoo": {"enabled": False}}}
        await db.flush()
        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="mach was",
            frame=frame, model_locality="eu", caps=ModelCaps(), max_steps=40,
        )

    joined = "\n".join(msg.content for msg in pre.messages if isinstance(msg.content, str))
    assert "- Systems you can reach: microsoft365\n" in joined


def _fixed_retrieval(monkeypatch: pytest.MonkeyPatch, queries: list[str]) -> None:
    """Memory and KB retrieval as fixed blocks, recording each query text."""

    async def _memory(*_a: Any, query_text: str, **_kw: Any) -> str:
        queries.append(query_text)
        return "MEMORY-BLOCK"

    async def _kb(*_a: Any, query_text: str, **_kw: Any) -> tuple[str, bool]:
        queries.append(query_text)
        return "KB-BLOCK", False

    monkeypatch.setattr("oc8.agent.preamble.retrieve_context", _memory)
    monkeypatch.setattr("oc8.agent.preamble.retrieve_kb_context", _kb)


async def _copilot_chat(
    db: Any, tenant: uuid.UUID, turns: list[tuple[str, str]]
) -> tuple[m.Agent, m.Task, m.AgentRun, ChatReplay]:
    """The Copilot answering a chat whose path so far is `turns`."""
    assistant, task, run = await _copilot_with_person(db, tenant, seats=[])
    session = await db.get(m.ChatSession, uuid.UUID(run.context["chat_session_id"]))
    parent: m.ChatMessage | None = None
    for role, content in turns:
        message = m.ChatMessage(
            tenant_id=tenant, session_id=session.id, role=role, content=content,
            parent_id=parent.id if parent is not None else None,
        )
        db.add(message)
        await db.flush()
        parent = message
    assert parent is not None
    session.current_leaf_id = parent.id
    run.context = {
        **run.context, "task": "User: NEW-TURN", "turn_text": "NEW-TURN",
        "turn_role": "user", "transcript_until": str(parent.id),
    }
    await db.flush()
    return assistant, task, run, ChatReplay(session.id, parent.id)


async def test_chat_replay_order(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _direct_access(*_a: Any, **_kw: Any) -> str:
        return "DIRECT-ACCESS-BLOCK"

    monkeypatch.setattr("oc8.copilot.direct_access.direct_access_block", _direct_access)
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _copilot_chat(
            db, tenant, [("user", "OLD-QUESTION"), ("assistant", "OLD-ANSWER")]
        )
        pre = await build_run_preamble(
            db, agent=assistant, tenant_id=tenant, task_text="User: NEW-TURN", frame={},
            model_locality="cloud", caps=ModelCaps(), max_steps=40, task=task,
            run_id=run.id, replay=replay,
        )

    roles = [msg.role for msg in pre.messages]
    first_turn = roles.index("user")
    assert set(roles[:first_turn]) == {"system"}
    assert roles[first_turn:] == ["user", "assistant", "user"]
    assert [msg.content for msg in pre.messages[first_turn:-1]] == ["OLD-QUESTION", "OLD-ANSWER"]
    systems = "\n".join(str(msg.content) for msg in pre.messages[:first_turn])
    for block in ("MEMORY-BLOCK", "KB-BLOCK", "DIRECT-ACCESS-BLOCK", "Every agent in this tenant"):
        assert block not in systems, f"{block} is per turn, not part of the stable system prefix"
    final = pre.messages[-1].content
    assert isinstance(final, str)
    assert final.startswith("# Run context")
    order = [
        final.index(piece)
        for piece in ("MEMORY-BLOCK", "KB-BLOCK", "DIRECT-ACCESS-BLOCK", "NEW-TURN")
    ]
    assert order == sorted(order)
    assert final.endswith("\n\nNEW-TURN"), "the new turn is the person's message, unlabelled"


async def test_non_chat_preamble_is_unchanged(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trigger run's preamble keeps today's order: system prompt, rule,
    memory and KB as system messages, then run context and task as two user
    messages."""
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _solo_agent(db, tenant)
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, source="event", state="running",
            context={"task": "Invoice arrived"},
        )
        db.add(run)
        await db.flush()
        kwargs: dict[str, Any] = dict(
            agent=agent, tenant_id=tenant, task_text="Invoice arrived", frame={},
            model_locality="cloud", caps=ModelCaps(), max_steps=40, run_id=run.id,
        )
        pre = await build_run_preamble(db, **kwargs)
        explicit = await build_run_preamble(db, **kwargs, replay=None)
        prompt = system_prompt(agent, caps=ModelCaps(), tenant_name="the organization")

    from oc8.agent.provenance import RULE

    assert [(msg.role, msg.content) for msg in pre.messages[:4]] == [
        ("system", prompt),
        ("system", RULE),
        ("system", "MEMORY-BLOCK"),
        ("system", "KB-BLOCK"),
    ]
    assert [msg.role for msg in pre.messages[4:]] == ["user", "user"]
    assert str(pre.messages[4].content).startswith("# Run context\n")
    assert "- Origin: trigger\n" in str(pre.messages[4].content)
    assert pre.messages[5].content == "Invoice arrived"
    assert len(pre.messages) == 6
    assert [(msg.role, msg.content) for msg in explicit.messages] == [
        (msg.role, msg.content) for msg in pre.messages
    ]


async def test_retrieval_query_is_new_message_plus_last_answer(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: list[str] = []
    _fixed_retrieval(monkeypatch, queries)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _copilot_chat(
            db, tenant,
            [("user", "OLD-QUESTION"), ("assistant", "OLD-ANSWER"), ("followup", "PING")],
        )
        await build_run_preamble(
            db, agent=assistant, tenant_id=tenant, task_text="User: NEW-TURN", frame={},
            model_locality="cloud", caps=ModelCaps(), max_steps=40, task=task,
            run_id=run.id, replay=replay,
        )
    assert queries == ["NEW-TURN\nOLD-ANSWER", "NEW-TURN\nOLD-ANSWER"]


async def _preamble_for(
    db: Any, tenant: uuid.UUID, assistant: Any, task: Any, run: Any, replay: ChatReplay,
    *, locality: str = "cloud",
) -> Any:
    return await build_run_preamble(
        db, agent=assistant, tenant_id=tenant, task_text="User: NEW-TURN", frame={},
        model_locality=locality, caps=ModelCaps(), max_steps=40, task=task,
        run_id=run.id, replay=replay,
    )


async def test_system_prefix_is_byte_equal_across_two_turns(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory, KB and the run context (Now, person) differ per turn; none of
    them is in the system part, so two turns of one chat share the prefix."""
    blocks = iter(["MEMORY-ONE", "KB-ONE", "MEMORY-TWO", "KB-TWO"])

    async def _memory(*_a: Any, **_kw: Any) -> str:
        return next(blocks)

    async def _kb(*_a: Any, **_kw: Any) -> tuple[str, bool]:
        return next(blocks), False

    monkeypatch.setattr("oc8.agent.preamble.retrieve_context", _memory)
    monkeypatch.setattr("oc8.agent.preamble.retrieve_kb_context", _kb)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _copilot_chat(db, tenant, [("user", "Q1")])
        first = await _preamble_for(db, tenant, assistant, task, run, replay)
        session = await db.get(m.ChatSession, replay.session_id)
        parent = session.current_leaf_id
        for role, content in (("assistant", "A1"), ("user", "Q2")):
            message = m.ChatMessage(
                tenant_id=tenant, session_id=session.id, role=role, content=content,
                parent_id=parent,
            )
            db.add(message)
            await db.flush()
            parent = message.id
        session.current_leaf_id = parent
        second = await _preamble_for(
            db, tenant, assistant, task, run, ChatReplay(session.id, parent)
        )

    def _system(pre: Any) -> list[tuple[str, Any]]:
        return [(msg.role, msg.content) for msg in pre.messages if msg.role == "system"]

    assert _system(first) == _system(second)
    assert "MEMORY-ONE" in str(first.messages[-1].content)
    assert "MEMORY-TWO" in str(second.messages[-1].content)


async def test_breakpoints_at_end_of_system_and_end_of_replay(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _copilot_chat(
            db, tenant, [("user", "OLD-QUESTION"), ("assistant", "OLD-ANSWER")]
        )
        pre = await _preamble_for(db, tenant, assistant, task, run, replay)
    marked = [i for i, msg in enumerate(pre.messages) if msg.cache_breakpoint]
    first_turn = [msg.role for msg in pre.messages].index("user")
    assert marked == [first_turn - 1, len(pre.messages) - 2]
    assert pre.messages[marked[1]].content == "OLD-ANSWER"


async def test_first_turn_marks_only_the_system_prefix_and_other_runs_none(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run = await _copilot_with_person(db, tenant, seats=[])
        first = await _preamble_for(
            db, tenant, assistant, task, run, ChatReplay(uuid.uuid4(), None)
        )
        agent = await _solo_agent(db, tenant)
        trigger = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, source="event", state="running",
            context={"task": "Invoice arrived"},
        )
        db.add(trigger)
        await db.flush()
        other = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="Invoice arrived", frame={},
            model_locality="cloud", caps=ModelCaps(), max_steps=40, run_id=trigger.id,
        )
    marked = [msg for msg in first.messages if msg.cache_breakpoint]
    assert len(marked) == 1 and marked[0].role == "system"
    assert not any(msg.cache_breakpoint for msg in other.messages)


async def _restricted_chat(db: Any, tenant: uuid.UUID) -> tuple[Any, Any, Any, ChatReplay]:
    assistant, task, run, replay = await _copilot_chat(
        db, tenant, [("user", "Q"), ("assistant", "SECRET-ANSWER")]
    )
    secret = await db.get(m.ChatMessage, replay.until_id)
    secret.restricted = True
    await db.flush()
    return assistant, task, run, replay


async def test_restricted_answer_replayed_to_a_local_model_marks_the_run_restricted(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _restricted_chat(db, tenant)
        local = await _preamble_for(db, tenant, assistant, task, run, replay, locality="local")
        cloud = await _preamble_for(db, tenant, assistant, task, run, replay, locality="cloud")
    assert local.contains_restricted is True
    assert any(msg.content == "SECRET-ANSWER" for msg in local.messages)
    assert cloud.contains_restricted is False
    assert not any("SECRET-ANSWER" in str(msg.content) for msg in cloud.messages)


async def test_restricted_kb_hit_still_marks_the_run_restricted_on_the_replay_path(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _memory(*_a: Any, **_kw: Any) -> str:
        return ""

    async def _kb(*_a: Any, **_kw: Any) -> tuple[str, bool]:
        return "KB-BLOCK", True

    monkeypatch.setattr("oc8.agent.preamble.retrieve_context", _memory)
    monkeypatch.setattr("oc8.agent.preamble.retrieve_kb_context", _kb)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _copilot_chat(db, tenant, [("user", "Q")])
        pre = await _preamble_for(db, tenant, assistant, task, run, replay)
    assert pre.contains_restricted is True


async def test_failing_flattened_fallback_keeps_the_task_text(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("replay unreadable")

    monkeypatch.setattr("oc8.agent.preamble.replay_messages", _boom)
    monkeypatch.setattr("oc8.agent.preamble.flattened_task_text_for_model", _boom)
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _copilot_chat(db, tenant, [("user", "Q")])
        pre = await _preamble_for(db, tenant, assistant, task, run, replay)
    assert pre.messages[-1].content == "User: NEW-TURN"


@pytest.mark.parametrize(
    ("locality", "shown", "restricted"),
    [("cloud", "[restricted answer withheld from this model]", False),
     ("local", "SECRET-ANSWER", True)],
)
async def test_the_flattened_fallback_withholds_a_restricted_answer_from_a_cloud_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch,
    locality: str, shown: str, restricted: bool,
) -> None:
    """When the native replay fails, the run falls back to the flattened
    transcript -- which printed a restricted answer verbatim, to whichever
    model the in-process run was on."""

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("replay unreadable")

    monkeypatch.setattr("oc8.agent.preamble.replay_messages", _boom)
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _restricted_chat(db, tenant)
        pre = await _preamble_for(db, tenant, assistant, task, run, replay, locality=locality)
    final = pre.messages[-1].content
    assert isinstance(final, str)
    assert f"You (assistant): {shown}\n" in final
    if locality != "local":
        assert not any("SECRET-ANSWER" in str(msg.content) for msg in pre.messages)
    assert pre.contains_restricted is restricted


@pytest.mark.parametrize(
    ("digest", "told"), [("search_records {} → ok: 3 rows", None), (None, "[no tool calls yet]")]
)
async def test_a_resume_follows_the_turn_and_keeps_the_cached_prefix(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    digest: str | None,
    told: str | None,
) -> None:
    """A resumed chat leg: the replayed turns and the new turn stay as they
    were, the run's own work and the decision follow them, and the cache
    breakpoints stay where a leg without a resume has them."""
    from oc8.agent.preamble import Resume

    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant, task, run, replay = await _copilot_chat(
            db, tenant, [("user", "OLD-QUESTION"), ("assistant", "OLD-ANSWER")]
        )
        plain = await _preamble_for(db, tenant, assistant, task, run, replay)
        resumed = await build_run_preamble(
            db, agent=assistant, tenant_id=tenant, task_text="User: NEW-TURN", frame={},
            model_locality="cloud", caps=ModelCaps(), max_steps=40, task=task,
            run_id=run.id, replay=replay,
            resume=Resume(own_work_digest=digest, instruction="RESUME-INSTRUCTION"),
        )
    head = resumed.messages[: len(plain.messages)]
    assert [(msg.role, msg.cache_breakpoint) for msg in head] == [
        (msg.role, msg.cache_breakpoint) for msg in plain.messages
    ]
    assert [msg.content for msg in head[:-1]] == [msg.content for msg in plain.messages[:-1]]
    assert str(head[-1].content).endswith("\n\nNEW-TURN")
    tail = resumed.messages[len(plain.messages) :]
    assert [(msg.role, msg.content) for msg in tail] == [
        ("assistant", told or digest),
        ("user", "RESUME-INSTRUCTION"),
    ]
    assert not any(msg.cache_breakpoint for msg in tail)
