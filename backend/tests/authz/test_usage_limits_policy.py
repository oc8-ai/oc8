"""Usage limits as part of a tool policy: "at most 50 of these calls a day".

A limit is policy data like `only` or `conditions` -- it travels on the stored
frame and narrowing, survives a round trip through JSON, and a narrowing can
only ever add one (the effective set is the union, exactly as with
`conditions`).
"""

from __future__ import annotations

import pytest

from oc8.authz.pdp import (
    ToolPolicy,
    UsageLimit,
    effective_tool_policies,
    has_usage_limit,
    usage_limits_for,
)

SENDING = {"tools": ["mail_send", "mail_reply", "mail_forward"], "max": 50, "per": "day"}


def test_a_limit_round_trips_through_the_stored_json() -> None:
    policy = ToolPolicy.from_json({"enabled": True, "read": True, "limits": [SENDING]})
    assert policy.limits == (
        UsageLimit(tools=("mail_send", "mail_reply", "mail_forward"), max=50, per="day"),
    )
    assert ToolPolicy.from_json(policy.to_json()) == policy
    assert policy.to_json()["limits"] == [SENDING]


def test_a_policy_without_limits_stores_an_empty_list() -> None:
    assert ToolPolicy.from_json({"enabled": True}).to_json()["limits"] == []


def test_per_defaults_to_a_day_and_hour_is_allowed() -> None:
    assert UsageLimit.from_json({"tools": ["a"], "max": 3}).per == "day"
    assert UsageLimit.from_json({"tools": ["a"], "max": 3, "per": "hour"}).per == "hour"


@pytest.mark.parametrize(
    "raw",
    [
        {"tools": [], "max": 1},
        {"tools": ["a"], "max": -1},
        {"tools": ["a"], "max": 1, "per": "week"},
        {"tools": [""], "max": 1},
        {"tools": ["a"], "max": True},
    ],
)
def test_a_malformed_limit_is_refused(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        UsageLimit.from_json(raw)


def test_the_counter_key_ignores_order_and_max() -> None:
    """Frame and narrowing may both limit the same tools with different
    maxima: both count the same calls, so they must share one counter."""
    a = UsageLimit(tools=("mail_send", "mail_reply"), max=50)
    b = UsageLimit(tools=("mail_reply", "mail_send"), max=10)
    assert a.key == b.key == "mail_reply/mail_send"


def test_limits_for_a_tool_are_only_those_naming_it() -> None:
    policy = ToolPolicy.from_json(
        {"enabled": True, "limits": [SENDING, {"tools": ["mail_move"], "max": 5}]}
    )
    assert [lim.key for lim in usage_limits_for(policy, "mail_reply")] == [
        "mail_forward/mail_reply/mail_send"
    ]
    assert usage_limits_for(policy, "mail_get") == ()
    assert usage_limits_for(None, "mail_reply") == ()


def test_a_narrowing_adds_a_limit_and_cannot_drop_the_frames() -> None:
    frame = {"tools": {"email": {"enabled": True, "read": True, "limits": [SENDING]}}}
    narrowing = {
        "tools": {
            "email": {
                "enabled": True,
                "read": True,
                "limits": [{"tools": ["mail_send"], "max": 5, "per": "hour"}],
            }
        }
    }
    eff = effective_tool_policies(frame, narrowing)["email"]
    assert {(lim.key, lim.max, lim.per) for lim in eff.limits} == {
        ("mail_forward/mail_reply/mail_send", 50, "day"),
        ("mail_send", 5, "hour"),
    }
    # An agent-only grant keeps its own limits.
    only_agent = effective_tool_policies({}, frame)["email"]
    assert only_agent.limits == ToolPolicy.from_json(frame["tools"]["email"]).limits


def test_has_usage_limit_reads_the_policy_of_the_calls_connection() -> None:
    policies = effective_tool_policies(
        {"tools": {"email": {"enabled": True, "read": True, "limits": [SENDING]}}}, {}
    )
    assert has_usage_limit(policies, connection_key="email", tool="mail_send")
    assert not has_usage_limit(policies, connection_key="email", tool="mail_get")
    assert not has_usage_limit(policies, connection_key=None, tool="mail_send")


def test_limits_that_are_not_a_list_are_refused() -> None:
    with pytest.raises(ValueError):
        ToolPolicy.from_json({"enabled": True, "limits": "fifty"})


def test_the_same_limit_in_frame_and_narrowing_counts_once_with_the_tighter_max() -> None:
    """An agent's narrowing often repeats the frame's limit (the editor saves
    what it shows). Counted twice, every call would use up two of the budget."""
    frame = {
        "tools": {
            "email": {
                "enabled": True,
                "modify": True,
                "limits": [
                    {"tools": ["mail_send", "mail_reply"], "max": 50},
                ],
            }
        }
    }
    narrowing = {
        "tools": {
            "email": {
                "enabled": True,
                "modify": True,
                "limits": [
                    {"tools": ["mail_reply", "mail_send"], "max": 20},
                    {"tools": ["mail_send"], "max": 5, "per": "hour"},
                ],
            }
        }
    }
    policy = effective_tool_policies(frame, narrowing)["email"]
    assert [lim.to_json() for lim in policy.limits] == [
        {"tools": ["mail_send", "mail_reply"], "max": 20, "per": "day"},
        {"tools": ["mail_send"], "max": 5, "per": "hour"},
    ]
    doubled = ToolPolicy(
        enabled=True,
        limits=(
            UsageLimit(tools=("mail_send",), max=9),
            UsageLimit(tools=("mail_send",), max=3),
        ),
    )
    assert [lim.max for lim in usage_limits_for(doubled, "mail_send")] == [3]


def test_a_narrowing_may_not_raise_the_departments_limit() -> None:
    """The tighter max is the one in force, so an agent row that shows a
    higher one would be a number nobody enforces: refused at write time."""
    from oc8.authz.pdp import narrowing_within_frame

    frame = {
        "tools": {
            "email": {
                "enabled": True,
                "modify": True,
                "limits": [{"tools": ["mail_send", "mail_reply"], "max": 50}],
            }
        }
    }

    def narrowing(*limits: dict[str, object]) -> dict[str, object]:
        return {"tools": {"email": {"enabled": True, "modify": True, "limits": list(limits)}}}

    raised = narrowing_within_frame(
        frame, narrowing({"tools": ["mail_reply", "mail_send"], "max": 80})
    )
    assert [v.reason for v in raised] == [
        "The department allows at most 50 per day for mail_send/mail_reply"
    ]
    assert (
        narrowing_within_frame(frame, narrowing({"tools": ["mail_send", "mail_reply"], "max": 20}))
        == []
    )
    assert narrowing_within_frame(frame, narrowing({"tools": ["mail_send"], "max": 80})) == []
    assert (
        narrowing_within_frame(
            frame, narrowing({"tools": ["mail_send", "mail_reply"], "max": 80, "per": "hour"})
        )
        == []
    )
