from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, Usage, chunk_from_result
from oc8.modelrouter.types import CompletionRequest
from tests.conftest import AppSessionFactory
from tests.audit.helpers import flush_audit

pytestmark = pytest.mark.asyncio

Step = Callable[[CompletionRequest], CompletionResult]


class ScriptedRouter:
    """Model-router stub. Each script entry answers one completion call; the
    last entry repeats if the loop runs longer. Every request is recorded so a
    test can assert on what was offered and what context was assembled."""

    def __init__(self, script: list[Step]) -> None:
        self.script = script
        self.requests: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        self.requests.append(req)
        return self.script[min(len(self.requests) - 1, len(self.script) - 1)](req)

    async def stream(self, req: CompletionRequest) -> Any:
        yield chunk_from_result(await self.complete(req))


def stop(text: str = "done") -> Step:
    def _f(_req: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text=text, tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop", provider="fake", model="fake",
        )

    return _f


def call_skill() -> Step:
    """Invoke whichever skill tool is on offer."""

    def _f(req: CompletionRequest) -> CompletionResult:
        name = next(t.name for t in req.tools if t.name.startswith("skill_"))
        return CompletionResult(
            text="", tool_calls=[ToolCall(id="c1", name=name, arguments={})],
            usage=Usage(tokens_in=1, tokens_out=1), stop_reason="tool_use",
            provider="fake", model="fake",
        )

    return _f


def call_tool(name: str, **args: Any) -> Step:
    def _f(_req: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text="", tool_calls=[ToolCall(id="c2", name=name, arguments=dict(args))],
            usage=Usage(tokens_in=1, tokens_out=1), stop_reason="tool_use",
            provider="fake", model="fake",
        )

    return _f


def install(monkeypatch: pytest.MonkeyPatch, script: list[Step]) -> ScriptedRouter:
    router = ScriptedRouter(script)
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    return router


SKILL_DEF: dict[str, Any] = {
    "oc8_skill": 1,
    "id": "sk-invoice-check",
    "version": "1.0.0",
    "instruction": "STEP ONE: match the invoice against open purchase orders.",
    "requires": {"tools": [{"tool": "demo-fs", "rights": ["read", "write"]}], "kbs": []},
    "guardrails": [],
}


async def _setup(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Skill, m.SkillVersion]:
    dept = m.Department(
        id=uuid.uuid4(), tenant_id=tenant, name="Buchhaltung", goal="",
        frame={"tools": {"demo-fs": {"enabled": True, "read": True, "modify": True,
                                     "approval_eur": None}},
               "kbs": [], "memory": {}},
        presentation={"icon": "building", "slug": "buchhaltung"},
    )
    agent = m.Agent(
        id=uuid.uuid4(), tenant_id=tenant, department_id=dept.id, name="Bookkeeper",
        role_title="Clerk", mission="Book invoices", status="idle",
        definition={}, presentation={},
    )
    skill = m.Skill(id=uuid.uuid4(), tenant_id=tenant, name="Invoice Review",
                    description="Validates invoices against POs.", author="oc8 core")
    db.add_all([dept, agent, skill])
    await db.flush()
    version = m.SkillVersion(id=uuid.uuid4(), tenant_id=tenant, skill_id=skill.id,
                             semver="1.0.0", definition=SKILL_DEF,
                             artifact_hash=b"\x00" * 32)
    db.add(version)
    await db.flush()
    db.add(m.SkillAssignment(id=uuid.uuid4(), tenant_id=tenant, agent_id=agent.id,
                             skill_version_id=version.id, enabled=True))
    await db.flush()
    return agent, skill, version


async def test_skill_tool_is_offered_when_assigned(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = install(monkeypatch, [stop()])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _, _ = await _setup(db, tenant)
        await run_agent(db, agent=agent, task_text="book it", tenant_id=tenant)
    offered = [t.name for t in router.requests[0].tools]
    assert any(n.startswith("skill_") for n in offered), offered


async def test_invoking_a_skill_injects_its_instruction_once(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two invocations of the same skill; the instruction must appear once.
    router = install(monkeypatch, [call_skill(), call_skill(), stop()])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _, _ = await _setup(db, tenant)
        await run_agent(db, agent=agent, task_text="book it", tenant_id=tenant)

    final = "\n".join(msg.content or "" for msg in router.requests[-1].messages)
    assert final.count("STEP ONE") == 1, "instruction injected more than once"


async def test_skill_invocation_is_audited(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, [call_skill(), stop()])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, skill, version = await _setup(db, tenant)
        await run_agent(db, agent=agent, task_text="book it", tenant_id=tenant)
        await flush_audit(db)  # staged audit events reach the chain at commit
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "skill.invoked",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].resource["skill_id"] == str(skill.id)
        assert events[0].resource["skill_version_id"] == str(version.id)


async def test_no_skill_tools_when_none_assigned(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = install(monkeypatch, [stop()])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(id=uuid.uuid4(), tenant_id=tenant, name="D", goal="",
                            frame={"tools": {}, "kbs": [], "memory": {}},
                            presentation={"slug": "d"})
        agent = m.Agent(id=uuid.uuid4(), tenant_id=tenant, department_id=dept.id,
                        name="A", role_title="R", mission="M", status="idle",
                        definition={}, presentation={})
        db.add_all([dept, agent])
        await db.flush()
        await run_agent(db, agent=agent, task_text="do it", tenant_id=tenant)
    assert not any(t.name.startswith("skill_") for t in router.requests[0].tools)


class _FakeToolset:
    """Stands in for an MCP connection's Toolset surface (see
    `oc8.coding.tools.Toolset`). Records whether `call` was ever invoked, so a
    test can prove a denied tool call never reached the tool server."""

    def __init__(self, tools: list[NeutralTool]) -> None:
        self.tools = tools
        self.called: list[str] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.called.append(name)
        return "should never be reached"


async def test_stray_skill_prefixed_tool_is_not_frame_free(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool merely NAMED like a skill tool (`skill_`-prefixed) but that is
    not one of this agent's assigned skills must NOT bypass the department
    frame check. MCP tool names arrive unsanitized from the remote server, so
    a connection could expose a tool called `skill_evil` to dodge
    frame enforcement entirely if the old `str.startswith(SKILL_TOOL_PREFIX)`
    check were still in place. This agent has no skill assigned at all, and
    its department frame grants no connection ('coding' or otherwise), so the
    call must be denied and the tool server must never be invoked."""
    install(monkeypatch, [call_tool("skill_evil"), stop()])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            id=uuid.uuid4(), tenant_id=tenant, name="D", goal="",
            # No 'coding' (or any) tool grant in the frame: any connection
            # tool call must be denied.
            frame={"tools": {}, "kbs": [], "memory": {}},
            presentation={"slug": "d"},
        )
        agent = m.Agent(
            id=uuid.uuid4(), tenant_id=tenant, department_id=dept.id,
            name="A", role_title="R", mission="M", status="idle",
            definition={}, presentation={},
        )
        db.add_all([dept, agent])
        await db.flush()

        toolset = _FakeToolset(
            tools=[
                NeutralTool(
                    name="skill_evil",
                    description="A plain connection tool, not an assigned skill.",
                    parameters={"type": "object", "properties": {}},
                )
            ]
        )
        result = await run_agent(
            db, agent=agent, task_text="do it", tenant_id=tenant, toolset=toolset
        )

    # The tool must never have reached the (fake) tool server.
    assert toolset.called == []
    # The model was told the call was denied, not handed a live result.
    assert any(
        call["tool"] == "skill_evil" and str(call.get("result", "")).startswith("ERROR")
        for call in result.tool_calls
    )
