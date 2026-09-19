from __future__ import annotations

import uuid
from typing import Any

from oc8 import models as m
from oc8.agent.engine import _authorize
from oc8.authz.pdp import Decision, Effect
from oc8.modelrouter import ToolCall


def _agent(threshold: float | None = None) -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="A",
        role_title="R",
        mission="M",
        status="idle",
        definition={},
        presentation={"approval_value_eur": threshold} if threshold else {},
    )


FRAME: dict[str, Any] = {
    "tools": {
        "odoo": {"enabled": True, "read": True, "modify": True,
                 "approval_eur": 2500},
        "email": {"enabled": True, "read": True, "modify": False,
                  "approval_eur": None},
    }
}


def _call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id="1", name=name, arguments=dict(args))


def _authz(tc: ToolCall, *, key: str | None = "odoo", scopes: dict[str, Any] | None = None,
           agent: m.Agent | None = None,
           skill_thresholds: tuple[float | None, ...] = (),
           value_spec: dict[str, Any] | None = None) -> Decision:
    from oc8.authz.pdp import effective_tool_policies

    # Value extraction is spec-driven in the neutral core: a connection's plugin
    # declares which argument key holds the value. Simulate that here so the
    # generic value-approval mechanism is exercised.
    return _authorize(
        agent or _agent(),
        tc,
        frame=FRAME,
        tool_policies=effective_tool_policies(FRAME, {}),
        connection_key=key,
        tool_scopes=scopes,
        skill_thresholds=skill_thresholds,
        value_spec=value_spec if value_spec is not None else {"direct_fields": ["value_eur"]},
    )


def test_tool_without_a_frame_entry_is_denied() -> None:
    d = _authz(_call("anything"), key="github")
    assert d.effect is Effect.DENY
    assert "github" in d.reason


def test_unclassified_tool_needs_write_and_is_denied_when_absent() -> None:
    # `email` grants read but not modify; an unclassified tool needs modify.
    d = _authz(_call("whatever"), key="email")
    assert d.effect is Effect.DENY
    assert "modify" in d.reason


def test_read_classified_tool_is_allowed_with_read_rights() -> None:
    d = _authz(_call("list_invoices"), key="email", scopes={"read": ["list_invoices"]})
    assert d.effect is Effect.ALLOW


def test_write_tool_allowed_where_the_frame_grants_write() -> None:
    d = _authz(_call("post_entry"), key="odoo")
    assert d.effect is Effect.ALLOW


def test_frame_threshold_raises_approval() -> None:
    d = _authz(_call("post_entry", value_eur=3000))
    assert d.effect is Effect.REQUIRE_APPROVAL


def test_skill_guardrail_can_be_stricter_than_the_frame() -> None:
    d = _authz(_call("post_entry", value_eur=200), skill_thresholds=(100.0,))
    assert d.effect is Effect.REQUIRE_APPROVAL
    assert "100" in d.reason


def test_agent_threshold_still_applies() -> None:
    d = _authz(_call("post_entry", value_eur=600), agent=_agent(threshold=500))
    assert d.effect is Effect.REQUIRE_APPROVAL


def test_memory_write_is_unaffected_by_the_frame_tool_check() -> None:
    frame = {**FRAME, "memory": {"department": ["read", "write"]}}
    from oc8.authz.pdp import effective_tool_policies

    d = _authorize(
        _agent(),
        _call("memory_write", content="hello", tier="department"),
        frame=frame,
        tool_policies=effective_tool_policies(frame, {}),
        connection_key="odoo",
        tool_scopes=None,
    )
    assert d.effect is not Effect.DENY


def test_delegate_task_is_unaffected_by_the_frame_tool_check() -> None:
    agent = _agent()
    agent.is_team_lead = True
    d = _authz(_call("delegate_task", task_text="go", agent_id=str(uuid.uuid4())), agent=agent)
    assert d.effect is Effect.ALLOW


def test_propose_change_is_allowed_on_a_connection_less_run() -> None:
    """The Assistant's chat run has no tool connection bound, so falling
    through to the frame check returns DENY ("no tool connection bound to this
    run"). execute_control_tool never reads that decision for propose_change
    and proceeds regardless -- so without this special case the audit chain
    records `deny` for a call that in fact succeeded, on the one tool whose
    entire purpose is an honest human-review trail."""
    agent = _agent()
    agent.is_tenant_assistant = True
    d = _authz(
        _call("propose_change", operation_type="department.create", payload={"name": "X"}),
        key=None,
        agent=agent,
    )
    assert d.effect is Effect.ALLOW


def test_decide_approval_is_allowed_on_a_connection_less_run() -> None:
    """Same shape as propose_change, immediately above: decide_approval also
    belongs to no connection, so without its own special case here every
    successful call would fall through to the frame check's DENY and be
    audited as a denial, even though execute_control_tool never reads this
    decision for decide_approval and dispatches it regardless."""
    agent = _agent()
    agent.is_tenant_assistant = True
    d = _authz(
        _call("decide_approval", approval_id=str(uuid.uuid4()), decision="approve"),
        key=None,
        agent=agent,
    )
    assert d.effect is Effect.ALLOW


def test_run_shell_is_allowed_on_a_connection_less_run() -> None:
    """Same shape as propose_change/decide_approval, above: run_shell also
    belongs to no connection -- only the builtin isolated shell ever offers
    it (internal_agent.py's offer_run_shell), and that runtime has no MCP
    connection of its own either -- so without its own special case here
    every successful call would fall through to the frame check's DENY and be
    audited as a denial, even though execute_control_tool never reads this
    decision for run_shell and dispatches it regardless."""
    d = _authz(_call("run_shell", command="echo hi"), key=None)
    assert d.effect is Effect.ALLOW
