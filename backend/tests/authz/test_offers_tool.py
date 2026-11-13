"""One rule for offer and authorise: `offers_tool` is True exactly when
`authorize_tool_call` would not deny the tool on frame grounds.

Live 2026-11-13 (tenant a3a24937, department "General"): the frame's
microsoft365 `only` named files/sites/word/excel/powerpoint tools, yet the
model was offered every mail_*/calendar_*/teams_* tool and spent its steps on
"'microsoft365' does not grant the tool 'mail_search' in this frame".
"""

from __future__ import annotations

import itertools

from oc8.authz.pdp import (
    Effect,
    ToolPolicy,
    authorize_tool_call,
    effective_tool_policies,
    offers_tool,
    reachable_connections,
    required_right,
)

M365_SCOPES = {
    "read": ["files_list", "files_search", "mail_search", "word_get_text"],
    "modify": ["files_upload", "mail_send", "word_replace_text"],
}
GENERAL_FRAME = {
    "tools": {
        "microsoft365": {
            "enabled": True,
            "read": True,
            "modify": True,
            "only": ["files_list", "files_search", "files_upload", "word_get_text"],
        }
    }
}
FRAME_DENIALS = {
    "connection_not_in_frame",
    "connection_disabled",
    "right_not_granted",
    "tool_not_in_frame",
}


def _offers(policies: dict[str, ToolPolicy], tool: str, key: str = "microsoft365") -> bool:
    return offers_tool(policies, connection_key=key, tool=tool, scopes=M365_SCOPES)


def test_the_frame_only_withholds_mail_and_offers_files() -> None:
    policies = effective_tool_policies(GENERAL_FRAME, {})
    assert _offers(policies, "files_list")
    assert _offers(policies, "files_upload")
    assert not _offers(policies, "mail_search")
    assert not _offers(policies, "mail_send")


def test_modify_off_withholds_the_writing_tools() -> None:
    policies = {"microsoft365": ToolPolicy(enabled=True, read=True, modify=False)}
    assert _offers(policies, "files_list")
    assert not _offers(policies, "files_upload")
    # Unlisted in the manifest's scopes: fail closed to modify, as the gate does.
    assert not _offers(policies, "some_unclassified_tool")


def test_the_agents_narrowing_only_narrows_further() -> None:
    narrowing = {"tools": {"microsoft365": {"enabled": True, "read": True, "only": ["files_list"]}}}
    policies = effective_tool_policies(GENERAL_FRAME, narrowing)
    assert _offers(policies, "files_list")
    assert not _offers(policies, "files_search")
    assert not _offers(policies, "files_upload")
    # Naming a tool the frame withholds cannot grant it.
    widen = {"tools": {"microsoft365": {"enabled": True, "read": True, "only": ["mail_search"]}}}
    assert not _offers(effective_tool_policies(GENERAL_FRAME, widen), "mail_search")


def test_a_switched_off_or_missing_connection_offers_nothing() -> None:
    off = {"microsoft365": ToolPolicy(enabled=False, read=True, modify=True)}
    assert not _offers(off, "files_list")
    assert not _offers({}, "files_list")
    assert not offers_tool(
        {"microsoft365": ToolPolicy(enabled=True, read=True)},
        connection_key=None,
        tool="files_list",
        scopes=M365_SCOPES,
    )


def test_a_tool_that_needs_approval_is_still_offered() -> None:
    policies = {
        "microsoft365": ToolPolicy(
            enabled=True,
            read=True,
            modify=True,
            approval_actions=frozenset({"files_upload"}),
            approval_eur=0,
        )
    }
    assert _offers(policies, "files_upload")
    decision = authorize_tool_call(
        policies=policies,
        connection_key="microsoft365",
        right="modify",
        value=None,
        tool="files_upload",
    )
    assert decision.effect is Effect.REQUIRE_APPROVAL


def test_offer_and_gate_agree_on_every_frame_ground() -> None:
    tools = ["files_list", "files_upload", "mail_search", "mail_send", "unclassified"]
    for enabled, read, modify, only in itertools.product(
        (True, False),
        (True, False),
        (True, False),
        (None, frozenset({"files_list", "mail_send"}), frozenset()),
    ):
        policies = {
            "microsoft365": ToolPolicy(enabled=enabled, read=read, modify=modify, only=only)
        }
        for tool in tools:
            decision = authorize_tool_call(
                policies=policies,
                connection_key="microsoft365",
                right=required_right(tool, M365_SCOPES),
                value=None,
                tool=tool,
            )
            denied_by_frame = decision.reason_code in FRAME_DENIALS
            assert _offers(policies, tool) is not denied_by_frame, (policies, tool, decision)


def test_a_hidden_tool_named_anyway_is_still_denied() -> None:
    decision = authorize_tool_call(
        policies=effective_tool_policies(GENERAL_FRAME, {}),
        connection_key="microsoft365",
        right=required_right("mail_search", M365_SCOPES),
        value=None,
        tool="mail_search",
    )
    assert decision.effect is Effect.DENY
    assert decision.reason == "'microsoft365' does not grant the tool 'mail_search' in this frame"


def test_reachable_connections_names_only_systems_that_offer_something() -> None:
    policies = {
        "microsoft365": ToolPolicy(enabled=True, read=True),
        "odoo": ToolPolicy(enabled=False, read=True, modify=True),
        "jira": ToolPolicy(enabled=True),
        "github": ToolPolicy(enabled=True, read=True, only=frozenset()),
        "slack": ToolPolicy(enabled=True, modify=True, only=frozenset({"post_message"})),
    }
    assert reachable_connections(policies) == ["microsoft365", "slack"]
