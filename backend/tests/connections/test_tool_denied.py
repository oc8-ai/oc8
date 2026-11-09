"""A tool call the department frame (or the agent's own narrowing) withholds:
the run records which tool and where it is allowed, and the model gets one
sentence it can pass on (oc8.connections.tool_denied)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.api.v1.run import run_to_dto
from oc8.authz.pdp import Decision, Effect, authorize_tool_call, effective_tool_policies
from oc8.connections.tool_denied import record_tool_denied, tool_denied
from tests.conftest import AppSessionFactory
from tests.connections.test_access import MS_CFG

ONLY_CALENDAR = {
    "tools": {
        "microsoft365": {
            "enabled": True,
            "read": True,
            "modify": True,
            "only": ["calendar_list"],
        }
    }
}
FULL = {"tools": {"microsoft365": {"enabled": True, "read": True, "modify": True}}}


def _agent(department_id: uuid.UUID | None = None) -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=department_id or uuid.uuid4(),
        name="Mira",
    )


def _decide(
    frame: dict[str, Any], narrowing: dict[str, Any], tool: str = "mail_search"
) -> Decision:
    return authorize_tool_call(
        policies=effective_tool_policies(frame, narrowing),
        connection_key="microsoft365",
        right="read",
        value=None,
        tool=tool,
    )


def _conn() -> m.McpConnection:
    return m.McpConnection(name="microsoft365", server_url="", transport="stdio", config=MS_CFG)


def test_a_tool_the_department_withholds_is_described_for_the_person() -> None:
    agent = _agent()
    decision = _decide(ONLY_CALENDAR, {})
    denied = tool_denied(decision, frame=ONLY_CALENDAR, agent=agent, conn=_conn())
    assert denied is not None
    assert denied.as_context() == {
        "connection": "microsoft365",
        "tool": "mail_search",
        "label": "Microsoft 365",
        "right": "read",
        "reason": "tool_not_in_frame",
        "scope": "department",
        "departmentId": str(agent.department_id),
        "agentId": str(agent.id),
    }


def test_the_model_is_told_in_one_sentence_what_to_tell_the_person() -> None:
    decision = _decide(ONLY_CALENDAR, {})
    denied = tool_denied(decision, frame=ONLY_CALENDAR, agent=_agent(), conn=_conn())
    assert denied is not None
    text = denied.model_message()
    assert text.startswith("ERROR: ")
    assert "department does not allow the tool 'mail_search'" in text
    assert "Microsoft 365" in text
    assert "administrator can enable it in the department's guardrails" in text
    assert "do not retry" in text
    # One sentence: no stray full stop before the end.
    assert text.rstrip(".").count(". ") == 0


def test_a_tool_the_agent_narrowing_withholds_points_at_the_agent() -> None:
    """The frame grants it; only the agent's own guardrails withhold it -- the
    department's guardrails would be the wrong place to send anyone."""
    narrowing = {"tools": {"microsoft365": {"enabled": True, "read": False, "modify": False}}}
    decision = _decide(FULL, narrowing)
    assert decision.reason_code == "right_not_granted"
    denied = tool_denied(decision, frame=FULL, agent=_agent(), conn=None)
    assert denied is not None
    assert denied.scope == "agent"
    assert denied.label is None
    text = denied.model_message()
    assert "this agent's guardrails do not allow the tool 'mail_search'" in text
    assert "agent's guardrails" in text


def test_a_connection_the_department_does_not_grant_at_all() -> None:
    decision = authorize_tool_call(
        policies=effective_tool_policies({"tools": {}}, {}),
        connection_key="microsoft365",
        right="read",
        value=None,
        tool=None,
    )
    denied = tool_denied(decision, frame={"tools": {}}, agent=_agent(), conn=None)
    assert denied is not None
    assert denied.reason == "connection_not_in_frame"
    assert denied.scope == "department"
    assert "the connection 'microsoft365'" in denied.model_message()


@pytest.mark.parametrize(
    "decision",
    [
        Decision(Effect.ALLOW),
        Decision(Effect.DENY, "operator rejected this action"),
        Decision(Effect.DENY, "no tool connection bound to this run"),
        Decision(
            Effect.REQUIRE_APPROVAL,
            "x",
            reason_code="value_threshold_exceeded",
            context={"threshold": 1.0, "actual": 2.0},
        ),
    ],
)
def test_anything_but_a_frame_denial_is_not_described(decision: Decision) -> None:
    assert tool_denied(decision, frame=FULL, agent=_agent(), conn=None) is None


@pytest.mark.asyncio
async def test_the_denial_is_recorded_on_the_run_and_in_its_dto(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame=ONLY_CALENDAR)
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Mira")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context={"a": 1})
        db.add(run)
        await db.flush()
        denied = tool_denied(
            _decide(ONLY_CALENDAR, {}), frame=ONLY_CALENDAR, agent=agent, conn=None
        )
        assert denied is not None
        ctx: dict[str, Any] = {"a": 1}
        await record_tool_denied(db, run, denied, ctx=ctx)
        run_id = run.id

    assert ctx["tool_denied"]["tool"] == "mail_search"
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["a"] == 1
        assert run.context["tool_denied"]["reason"] == "tool_not_in_frame"
        dto = run_to_dto(run)
    assert dto.tool_denied is not None
    assert dto.tool_denied["departmentId"] == str(dept.id)
    assert dto.model_dump(by_alias=True)["toolDenied"]["tool"] == "mail_search"
