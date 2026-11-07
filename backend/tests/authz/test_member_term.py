from __future__ import annotations

from typing import Any

from oc8.authz.pdp import (
    Decision,
    Effect,
    authorize_tool_call,
    effective_tool_policies,
    member_term_violations,
    tool_policy_source,
)

FRAME: dict[str, Any] = {"tools": {"gmail": {"enabled": True, "read": True, "modify": True}}}
NARROWING: dict[str, Any] = {}


def _call(member: dict[str, Any] | None, right: str = "modify") -> Decision:
    policies = effective_tool_policies(FRAME, NARROWING, member)
    return authorize_tool_call(policies=policies, connection_key="gmail", right=right, value=None)


def test_no_member_behaves_as_today() -> None:
    assert _call(None).effect is Effect.ALLOW
    assert _call({}).effect is Effect.ALLOW


def test_member_can_switch_a_tool_off() -> None:
    d = _call({"tools": {"gmail": {"enabled": False, "read": True, "modify": True}}})
    assert d.effect is Effect.DENY


def test_member_read_only_denies_modify_but_allows_read() -> None:
    member = {"tools": {"gmail": {"enabled": True, "read": True, "modify": False}}}
    assert _call(member).effect is Effect.DENY
    assert _call(member, right="read").effect is Effect.ALLOW


def test_member_ask_first_routes_to_member_only() -> None:
    member = {
        "tools": {
            "gmail": {
                "enabled": True,
                "read": True,
                "modify": True,
                "approval_actions": ["modify"],
            }
        }
    }
    d = _call(member)
    assert d.effect is Effect.REQUIRE_APPROVAL
    assert d.approval_from == frozenset({"member"})


def test_department_and_member_both_require_approval() -> None:
    entry = {"enabled": True, "read": True, "modify": True, "approval_actions": ["modify"]}
    d = authorize_tool_call(
        policies=effective_tool_policies(
            {"tools": {"gmail": entry}}, {}, {"tools": {"gmail": entry}}
        ),
        connection_key="gmail",
        right="modify",
        value=None,
    )
    assert d.approval_from == frozenset({"member", "department"})


def test_department_only_approval_stays_department() -> None:
    frame = {"tools": {"gmail": {"enabled": True, "read": True, "modify": True, "approval_eur": 0}}}
    d = authorize_tool_call(
        policies=effective_tool_policies(frame, {}, None),
        connection_key="gmail",
        right="modify",
        value=10,
    )
    assert d.approval_from == frozenset({"department"})


def test_member_cannot_grant_a_tool_the_department_lacks() -> None:
    member = {"tools": {"calendar": {"enabled": True, "read": True, "modify": True}}}
    policies = effective_tool_policies(FRAME, NARROWING, member)
    assert "calendar" not in policies


def test_member_cannot_loosen_or_pin_a_login() -> None:
    frame = {"tools": {"gmail": {"enabled": True, "read": True, "modify": False}}}
    member = {
        "tools": {"gmail": {"enabled": True, "read": True, "modify": True, "connection_id": "x"}}
    }
    p = effective_tool_policies(frame, {}, member)["gmail"]
    assert p.modify is False
    assert p.connection_id is None


def test_member_condition_deny_wins() -> None:
    member = {
        "tools": {
            "gmail": {
                "enabled": True,
                "read": True,
                "modify": True,
                "conditions": [
                    {
                        "attribute": "recipient_domain",
                        "datatype": "string",
                        "operator": "!=",
                        "value": "firma.de",
                        "then": "deny",
                    }
                ],
            }
        }
    }
    policies = effective_tool_policies(FRAME, NARROWING, member)
    d = authorize_tool_call(
        policies=policies,
        connection_key="gmail",
        right="modify",
        value=None,
        attributes={"recipient_domain": "extern.com"},
    )
    assert d.effect is Effect.DENY


def test_violations_flag_loosening_and_unknown_keys() -> None:
    base = effective_tool_policies(
        {"tools": {"gmail": {"enabled": True, "read": True, "modify": False}}}, {}
    )
    v = member_term_violations(
        base,
        {
            "tools": {
                "gmail": {"enabled": True, "read": True, "modify": True},
                "calendar": {"enabled": True, "read": True, "modify": False},
            }
        },
    )
    assert {x.tool_key for x in v} == {"gmail", "calendar"}


def test_source_reports_member_first() -> None:
    src = tool_policy_source(
        "gmail",
        frame=FRAME,
        capa_defaults=None,
        narrowing_overridden_keys=["gmail"],
        member_keys=["gmail"],
    )
    assert src == "member"
