"""`Decision.reason_code`/`context` (dev session 2026-09-11) give the
approval-pane UI a stable, translatable identifier plus structured
parameters for the three REQUIRE_APPROVAL paths in `authorize_tool_call`,
instead of forcing it to parse or display `reason`'s raw English sentence
verbatim. Every DENY/ALLOW path is untouched -- `reason_code` stays None and
`context` stays empty for those, which these tests also pin down so a future
change doesn't quietly start populating them there too.
"""

from __future__ import annotations

from oc8.authz.pdp import Condition, Effect, ToolPolicy, authorize_tool_call


def _policies(**kw: ToolPolicy) -> dict[str, ToolPolicy]:
    return dict(kw)


def test_a_condition_match_reports_condition_matched_with_its_terms() -> None:
    d = authorize_tool_call(
        policies=_policies(
            odoo=ToolPolicy(
                enabled=True,
                modify=True,
                conditions=(Condition(attribute="order_value", operator=">", value=5000),),
            )
        ),
        connection_key="odoo",
        right="modify",
        value=None,
        attributes={"order_value": 12480},
    )
    assert d.effect is Effect.REQUIRE_APPROVAL
    assert d.reason_code == "condition_matched"
    assert d.context == {
        "attribute": "order_value",
        "operator": ">",
        "threshold": 5000,
        "actual": 12480,
    }


def test_approval_actions_reports_always_requires_approval_with_its_terms() -> None:
    d = authorize_tool_call(
        policies=_policies(
            odoo=ToolPolicy(enabled=True, modify=True, approval_actions=frozenset({"modify"}))
        ),
        connection_key="odoo",
        right="modify",
        value=None,
        tool="res_partner_write",
    )
    assert d.effect is Effect.REQUIRE_APPROVAL
    assert d.reason_code == "always_requires_approval"
    assert d.context == {"right": "modify", "tool": "res_partner_write", "connection": "odoo"}


def test_a_value_threshold_reports_value_threshold_exceeded_with_its_terms() -> None:
    d = authorize_tool_call(
        policies=_policies(odoo=ToolPolicy(enabled=True, modify=True, approval_eur=2500)),
        connection_key="odoo",
        right="modify",
        value=3000.0,
    )
    assert d.effect is Effect.REQUIRE_APPROVAL
    assert d.reason_code == "value_threshold_exceeded"
    assert d.context == {"threshold": 2500.0, "actual": 3000.0}


def test_a_plain_allow_has_no_reason_code_or_context() -> None:
    d = authorize_tool_call(
        policies=_policies(odoo=ToolPolicy(enabled=True, modify=True)),
        connection_key="odoo",
        right="modify",
        value=None,
    )
    assert d.effect is Effect.ALLOW
    assert d.reason_code is None
    assert d.context == {}


def test_a_deny_without_a_frame_reason_has_no_reason_code_or_context() -> None:
    d = authorize_tool_call(
        policies=_policies(),
        connection_key=None,
        right="modify",
        value=None,
    )
    assert d.effect is Effect.DENY
    assert d.reason_code is None
    assert d.context == {}


# The four frame denials carry a code and their terms, so a run can tell the
# person which tool the department withholds and where an admin allows it.
# The decision itself (DENY, and its reason text) is unchanged.


def test_a_connection_missing_from_the_frame_reports_connection_not_in_frame() -> None:
    d = authorize_tool_call(
        policies=_policies(),
        connection_key="odoo",
        right="modify",
        value=None,
        tool="create_record",
    )
    assert d.effect is Effect.DENY
    assert d.reason == "no grant for 'odoo' in this frame"
    assert d.reason_code == "connection_not_in_frame"
    assert d.context == {"connection": "odoo", "tool": "create_record", "right": "modify"}


def test_a_disabled_connection_reports_connection_disabled() -> None:
    d = authorize_tool_call(
        policies=_policies(odoo=ToolPolicy(enabled=False, read=True, modify=True)),
        connection_key="odoo",
        right="read",
        value=None,
        tool="search_records",
    )
    assert d.effect is Effect.DENY
    assert d.reason == "'odoo' is disabled in this frame"
    assert d.reason_code == "connection_disabled"
    assert d.context == {"connection": "odoo", "tool": "search_records", "right": "read"}


def test_a_missing_right_reports_right_not_granted() -> None:
    d = authorize_tool_call(
        policies=_policies(odoo=ToolPolicy(enabled=True, read=True, modify=False)),
        connection_key="odoo",
        right="modify",
        value=None,
        tool="create_record",
    )
    assert d.effect is Effect.DENY
    assert d.reason == "'odoo' does not grant 'modify'"
    assert d.reason_code == "right_not_granted"
    assert d.context == {"connection": "odoo", "tool": "create_record", "right": "modify"}


def test_a_tool_outside_only_reports_tool_not_in_frame() -> None:
    d = authorize_tool_call(
        policies=_policies(
            microsoft365=ToolPolicy(enabled=True, read=True, only=frozenset({"calendar_list"}))
        ),
        connection_key="microsoft365",
        right="read",
        value=None,
        tool="mail_search",
    )
    assert d.effect is Effect.DENY
    assert d.reason == "'microsoft365' does not grant the tool 'mail_search' in this frame"
    assert d.reason_code == "tool_not_in_frame"
    assert d.context == {"connection": "microsoft365", "tool": "mail_search", "right": "read"}
