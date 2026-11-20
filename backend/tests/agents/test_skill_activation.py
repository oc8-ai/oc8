from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import NeutralTool
from tests.agents.test_skill_invocation import (
    SKILL_DEF,
    _FakeToolset,
    _setup,
    call_skill,
    call_tool,
    install,
    stop,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_offer_narrows_to_the_active_skills_tools(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SKILL_DEF's requires.tools names only "demo-fs"; a second connection tool
    # ("other-tool") is unrelated to the skill and must drop out of the offer
    # once the skill activates -- narrowing acts on the connection tools, not
    # on the skill-invocation tools themselves (those stay addressable so a
    # re-invoke of an already-active skill still no-ops instead of naming an
    # unknown tool).
    router = install(monkeypatch, [call_skill(), stop()])
    tenant = uuid.uuid4()
    toolset = _FakeToolset(
        tools=[
            NeutralTool(
                name="demo-fs", description="required by the skill",
                parameters={"type": "object", "properties": {}},
            ),
            NeutralTool(
                name="other-tool", description="not required by the skill",
                parameters={"type": "object", "properties": {}},
            ),
        ]
    )
    async with app_session(tenant) as db:
        agent, _, _ = await _setup(db, tenant)
        await run_agent(
            db, agent=agent, task_text="book it", tenant_id=tenant, toolset=toolset
        )

    first = [t.name for t in router.requests[0].tools]
    second = [t.name for t in router.requests[1].tools]
    # Both connection tools offered before activation.
    assert "demo-fs" in first
    assert "other-tool" in first
    # Once the skill is active, the offer narrows to its required tool only.
    assert "demo-fs" in second
    assert "other-tool" not in second
    # The skill-invocation tool itself stays addressable (Task 5 invariant):
    # a re-invoke must still be able to name it and hit the no-op branch.
    assert any(n.startswith("skill_") for n in second)


async def test_usage_is_attributed_to_the_invoked_skill(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, [call_skill(), stop()])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, skill, version = await _setup(db, tenant)
        await run_agent(db, agent=agent, task_text="book it", tenant_id=tenant)
        rows = (
            (
                await db.execute(
                    select(m.TokenUsageRecord)
                    .where(m.TokenUsageRecord.tenant_id == tenant)
                    .order_by(m.TokenUsageRecord.ts)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) >= 2
        # The first completion happened before any invocation.
        assert rows[0].skill_id is None
        # A later one is attributed to the skill the model invoked.
        assert any(r.skill_id == skill.id and r.skill_version_id == version.id for r in rows)


async def test_a_skill_guardrail_tightens_the_approval_threshold(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded = {
        **SKILL_DEF,
        "guardrails": [
            {"type": "value_threshold", "action": "send_action", "metric": "value_eur",
             "gt": 100, "then": "require_approval"}
        ],
    }
    install(monkeypatch, [call_skill(), call_tool("fs_write", value_eur=500), stop()])
    tenant = uuid.uuid4()
    # A tool call is only ever authorized against a bound connection (§5.3): a
    # run with no toolset/mcp_conn always DENYs before any threshold is even
    # considered ("no tool connection bound to this run"). To exercise the
    # guardrail specifically (as opposed to the connection-missing DENY), grant
    # the 'coding' connection the frame otherwise wouldn't have, with no frame
    # threshold of its own -- so REQUIRE_APPROVAL can only come from the
    # skill's guardrail.
    toolset = _FakeToolset(
        tools=[
            NeutralTool(
                name="fs_write", description="write a file",
                parameters={"type": "object", "properties": {}},
            ),
        ]
    )
    async with app_session(tenant) as db:
        agent, _, version = await _setup(db, tenant)
        version.definition = guarded
        department = await db.get(m.Department, agent.department_id)
        assert department is not None
        department.frame = {
            **department.frame,
            "tools": {
                **department.frame["tools"],
                "coding": {
                    "enabled": True, "read": True, "modify": True,
                    "approval_eur": None,
                },
            },
        }
        await db.flush()
        result = await run_agent(
            db, agent=agent, task_text="book it", tenant_id=tenant, toolset=toolset
        )
        assert result.status == "waiting_for_approval"


async def _guarded_run(app_session, monkeypatch, pre_decided):
    """Same setup as the threshold test, but resume with a pre-decided verdict on
    the exact fs_write call. Returns the RunResult."""
    from oc8.agent.engine import _call_sig
    from oc8.modelrouter import ToolCall

    guarded = {
        **SKILL_DEF,
        "guardrails": [
            {"type": "value_threshold", "action": "send_action", "metric": "value_eur",
             "gt": 100, "then": "require_approval"}
        ],
    }
    install(monkeypatch, [call_skill(), call_tool("fs_write", value_eur=500), stop()])
    tenant = uuid.uuid4()
    toolset = _FakeToolset(
        tools=[
            NeutralTool(
                name="fs_write", description="write a file",
                parameters={"type": "object", "properties": {}},
            ),
        ]
    )
    sig = _call_sig(ToolCall(id="c2", name="fs_write", arguments={"value_eur": 500}))
    async with app_session(tenant) as db:
        agent, _, version = await _setup(db, tenant)
        version.definition = guarded
        department = await db.get(m.Department, agent.department_id)
        department.frame = {
            **department.frame,
            "tools": {
                **department.frame["tools"],
                "coding": {"enabled": True, "read": True, "modify": True,
                           "approval_eur": None},
            },
        }
        await db.flush()
        return await run_agent(
            db, agent=agent, task_text="resume", tenant_id=tenant, toolset=toolset,
            pre_decided={sig: pre_decided},
        ), toolset


async def test_pre_decided_approve_executes_the_held_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, toolset = await _guarded_run(app_session, monkeypatch, "approve")
    # No longer suspends: the call ran and the run completed.
    assert result.status == "done"
    assert any(c["tool"] == "fs_write" for c in result.tool_calls)
    assert "fs_write" in toolset.called


async def test_pre_decided_reject_skips_the_held_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, toolset = await _guarded_run(app_session, monkeypatch, "reject")
    assert result.status == "done"
    # Rejected: the tool was NOT executed against the toolset.
    assert "fs_write" not in toolset.called
