"""Copilot direct access at the PEP (design §6): pure, decided per call from a
view the runtime reloads before every call."""

from __future__ import annotations

import uuid
from typing import Any

from oc8 import models as m
from oc8.agent.harness.pipeline import Harness
from oc8.agent.harness.stages.b_approval import posture
from oc8.agent.harness.stages.b_authorize import authorize
from oc8.agent.harness.stages.b_direct_access import (
    HOLDING_DEPARTMENT,
    NO_DIRECT_ACCESS,
    DirectAccessView,
    direct_access_decision,
    offers_direct,
)
from oc8.agent.harness.state import Ledger
from oc8.authz.pdp import (
    NOT_THIS_CONNECTION,
    Condition,
    ConditionDatatype,
    ConditionOperator,
    Effect,
    ToolPolicy,
)
from oc8.modelrouter import ToolCall

SCOPES: dict[str, list[str]] = {"read": ["search_records"], "modify": ["update_record"]}
SALES = uuid.uuid4()
SUPPORT = uuid.uuid4()
READ_WRITE = ToolPolicy(enabled=True, read=True, modify=True)
READ_ONLY = ToolPolicy(enabled=True, read=True, modify=False)


def _view(*departments: tuple[uuid.UUID, ToolPolicy]) -> DirectAccessView:
    return DirectAccessView(member_id=uuid.uuid4(), granted={"things": tuple(departments)})


def _decide(view: DirectAccessView, tool: str, **kw: Any) -> Any:
    return direct_access_decision(
        view,
        connection_key=kw.pop("key", "things"),
        tool=tool,
        scopes=SCOPES,
        value=kw.pop("value", None),
        attributes={},
    )


def _copilot() -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Copilot",
        is_tenant_assistant=True,
        is_team_lead=True,
        narrowing={},
        presentation={},
    )


def test_no_grant_is_denied() -> None:
    decision = _decide(NO_DIRECT_ACCESS, "search_records")
    assert decision.effect is Effect.DENY
    assert decision.reason_code == NOT_THIS_CONNECTION


def test_another_connections_grant_does_not_count() -> None:
    decision = _decide(_view((SALES, READ_WRITE)), "search_records", key="other")
    assert decision.effect is Effect.DENY


def test_a_read_with_a_grant_is_allowed() -> None:
    assert _decide(_view((SALES, READ_ONLY)), "search_records").effect is Effect.ALLOW


def test_a_write_needs_modify_in_some_department() -> None:
    assert _decide(_view((SALES, READ_ONLY)), "update_record").effect is Effect.DENY
    assert (
        _decide(_view((SALES, READ_ONLY), (SUPPORT, READ_WRITE)), "update_record").effect
        is Effect.ALLOW
    )


def test_a_department_gate_holds_the_call_and_names_its_department() -> None:
    gated = ToolPolicy(enabled=True, read=True, modify=True, approval_actions=frozenset({"modify"}))
    decision = _decide(_view((SALES, READ_WRITE), (SUPPORT, gated)), "update_record")
    assert decision.effect is Effect.REQUIRE_APPROVAL
    assert decision.context[HOLDING_DEPARTMENT] == str(SUPPORT)


def test_offers_follow_the_same_rule() -> None:
    view = _view((SALES, READ_ONLY))
    assert offers_direct(view, connection_key="things", tool="search_records", scopes=SCOPES)
    assert not offers_direct(view, connection_key="things", tool="update_record", scopes=SCOPES)
    assert not offers_direct(view, connection_key="other", tool="search_records", scopes=SCOPES)


def test_the_copilot_reaches_a_connection_only_through_the_view() -> None:
    """Its own department frame grants it nothing (§6): a frame that enables
    the tool does not let a call through without a grant."""
    frame_grants = {"things": READ_WRITE}
    call = ToolCall(id="c", name="search_records", arguments={})
    denied = authorize(
        _copilot(),
        call,
        frame={},
        tool_policies=frame_grants,
        connection_key="things",
        tool_scopes=SCOPES,
    )
    assert denied.effect is Effect.DENY and denied.reason_code == NOT_THIS_CONNECTION
    allowed = authorize(
        _copilot(),
        call,
        frame={},
        tool_policies={},
        connection_key="things",
        tool_scopes=SCOPES,
        direct_access=_view((SALES, READ_ONLY)),
    )
    assert allowed.effect is Effect.ALLOW


def test_a_connection_tool_named_like_a_core_tool_is_decided_as_the_connections() -> None:
    """A routed `fetch_url` is the connection's tool: no core ALLOW for it."""
    scopes = {"read": ["fetch_url"]}
    call = ToolCall(id="c", name="fetch_url", arguments={})
    denied = authorize(
        _copilot(),
        call,
        frame={},
        tool_policies={},
        connection_key="things",
        tool_scopes=scopes,
        routed=True,
    )
    assert denied.effect is Effect.DENY and denied.reason_code == NOT_THIS_CONNECTION


def test_an_ordinary_agent_is_untouched_by_the_view() -> None:
    plain = _copilot()
    plain.is_tenant_assistant = False
    decision = authorize(
        plain,
        ToolCall(id="c", name="search_records", arguments={}),
        frame={},
        tool_policies={"things": READ_ONLY},
        connection_key="things",
        tool_scopes=SCOPES,
    )
    assert decision.effect is Effect.ALLOW


def test_posture_asks_every_non_read_tier_when_told_to() -> None:
    assert posture("read", "autonomous", granted=False, always_ask_writes=True) == "allow"
    for tier in ("write", "destructive", "irreversible", "outward"):
        assert posture(tier, "autonomous", granted=True, always_ask_writes=True) == "ask"
    assert posture("write", "autonomous", granted=False) == "allow"


def test_the_gate_asks_for_a_direct_write_under_autonomous_autonomy() -> None:
    verdict = Harness().gate(
        ToolCall(id="c", name="update_record", arguments={}),
        tier="write",
        ledger=Ledger(),
        connection="things",
        config=None,
        autonomy="autonomous",
        granted=False,
        record_label="",
        identity=None,
        always_ask_writes=True,
    )
    assert verdict.effect == "ask"


def test_the_copilots_own_eur_threshold_still_applies_to_a_direct_call() -> None:
    """A department policy only adds gates (§1): the agent's approval_value_eur
    holds a direct call that the departments alone would let through."""
    agent = _copilot()
    agent.presentation = {"approval_value_eur": 100}
    call = ToolCall(id="c", name="update_record", arguments={"amount": 500})
    view = _view((SALES, READ_WRITE))
    held = authorize(
        agent,
        call,
        frame={},
        tool_policies={},
        connection_key="things",
        tool_scopes=SCOPES,
        value_spec={"direct_fields": ["amount"]},
        direct_access=view,
    )
    assert held.effect is Effect.REQUIRE_APPROVAL
    cheap = ToolCall(id="c", name="update_record", arguments={"amount": 5})
    allowed = authorize(
        agent,
        cheap,
        frame={},
        tool_policies={},
        connection_key="things",
        tool_scopes=SCOPES,
        value_spec={"direct_fields": ["amount"]},
        direct_access=view,
    )
    assert allowed.effect is Effect.ALLOW


def _deny_when_blocked() -> ToolPolicy:
    return ToolPolicy(
        enabled=True,
        read=True,
        modify=True,
        conditions=(
            Condition(
                attribute="kind",
                datatype=ConditionDatatype.STRING,
                operator=ConditionOperator.EQ,
                value="blocked",
                then=Effect.DENY,
            ),
        ),
    )


def _write(view: DirectAccessView, attributes: dict[str, Any], **kw: Any) -> Any:
    return direct_access_decision(
        view,
        connection_key="things",
        tool="update_record",
        scopes=SCOPES,
        value=kw.pop("value", None),
        attributes=attributes,
        **kw,
    )


def test_a_granting_departments_deny_condition_beats_another_departments_allow() -> None:
    view = _view((SALES, READ_WRITE), (SUPPORT, _deny_when_blocked()))
    assert _write(view, {"kind": "blocked"}).effect is Effect.DENY
    assert _write(view, {"kind": "fine"}).effect is Effect.ALLOW


def test_a_department_that_does_not_grant_the_right_is_skipped_not_strictest() -> None:
    view = _view((SALES, READ_WRITE), (SUPPORT, READ_ONLY))
    assert _write(view, {}).effect is Effect.ALLOW


def test_the_copilots_narrowing_denies_a_direct_write() -> None:
    narrowing = {"tools": {"things": {"enabled": True, "read": True, "modify": False}}}
    view = _view((SALES, READ_WRITE))
    assert _write(view, {}, narrowing=narrowing).effect is Effect.DENY
    assert not offers_direct(
        view, connection_key="things", tool="update_record", scopes=SCOPES, narrowing=narrowing
    )
    assert offers_direct(
        view, connection_key="things", tool="search_records", scopes=SCOPES, narrowing=narrowing
    )


def test_the_copilots_narrowing_threshold_holds_a_direct_call() -> None:
    narrowing = {
        "tools": {"things": {"enabled": True, "read": True, "modify": True, "approval_eur": 50}}
    }
    view = _view((SALES, ToolPolicy(enabled=True, read=True, modify=True, approval_eur=500)))
    assert _write(view, {}, value=100.0).effect is Effect.ALLOW
    held = _write(view, {}, value=100.0, narrowing=narrowing)
    assert held.effect is Effect.REQUIRE_APPROVAL
    assert HOLDING_DEPARTMENT not in held.context


def test_a_disabling_narrowing_withdraws_the_offer_and_a_narrowing_only_key_grants_nothing() -> (
    None
):
    off = {"tools": {"things": {"enabled": False}}}
    view = _view((SALES, READ_WRITE))
    assert not offers_direct(
        view, connection_key="things", tool="search_records", scopes=SCOPES, narrowing=off
    )
    only = {"tools": {"other": {"enabled": True, "read": True, "modify": True}}}
    decision = direct_access_decision(
        view,
        connection_key="other",
        tool="search_records",
        scopes=SCOPES,
        value=None,
        attributes={},
        narrowing=only,
    )
    assert decision.effect is Effect.DENY


def test_a_hold_from_the_agents_own_threshold_names_no_department() -> None:
    held = _write(_view((SALES, READ_WRITE)), {}, value=500.0, extra_thresholds=(100.0,))
    assert held.effect is Effect.REQUIRE_APPROVAL
    assert HOLDING_DEPARTMENT not in held.context


def test_a_departments_own_hold_names_it_even_with_an_extra_threshold() -> None:
    gated = ToolPolicy(enabled=True, read=True, modify=True, approval_actions=frozenset({"modify"}))
    held = _write(
        _view((SALES, READ_WRITE), (SUPPORT, gated)), {}, value=500.0, extra_thresholds=(100.0,)
    )
    assert held.context[HOLDING_DEPARTMENT] == str(SUPPORT)


def test_authorize_applies_the_copilots_pinned_narrowing_to_a_direct_call() -> None:
    agent = _copilot()
    agent.narrowing = {"tools": {"things": {"enabled": True, "read": True, "modify": False}}}
    decision = authorize(
        agent,
        ToolCall(id="c", name="update_record", arguments={}),
        frame={},
        tool_policies={},
        connection_key="things",
        tool_scopes=SCOPES,
        direct_access=_view((SALES, READ_WRITE)),
    )
    assert decision.effect is Effect.DENY


def test_a_narrowing_approval_action_holds_without_naming_a_department() -> None:
    narrowing = {
        "tools": {
            "things": {
                "enabled": True,
                "read": True,
                "modify": True,
                "approval_actions": ["update_record"],
            }
        }
    }
    held = _write(_view((SALES, READ_WRITE)), {}, narrowing=narrowing)
    assert held.effect is Effect.REQUIRE_APPROVAL
    assert HOLDING_DEPARTMENT not in held.context


def test_a_department_hold_is_named_even_when_the_narrowing_also_holds() -> None:
    gated = ToolPolicy(enabled=True, read=True, modify=True, approval_actions=frozenset({"modify"}))
    narrowing = {
        "tools": {
            "things": {
                "enabled": True,
                "read": True,
                "modify": True,
                "approval_actions": ["update_record"],
            }
        }
    }
    held = _write(_view((SALES, READ_WRITE), (SUPPORT, gated)), {}, narrowing=narrowing)
    assert held.context[HOLDING_DEPARTMENT] == str(SUPPORT)
