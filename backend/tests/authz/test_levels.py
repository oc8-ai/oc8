from __future__ import annotations

import pytest

from oc8.authz.levels import Level, has_limits, level_of, member_entry_for_level
from oc8.authz.pdp import ToolPolicy


@pytest.mark.parametrize(
    ("policy", "level"),
    [
        (ToolPolicy(enabled=False), "off"),
        (ToolPolicy(enabled=True, read=True, modify=False), "read_only"),
        (
            ToolPolicy(
                enabled=True, read=True, modify=True, approval_actions=frozenset({"modify"})
            ),
            "ask_first",
        ),
        (ToolPolicy(enabled=True, read=True, modify=True), "allowed"),
    ],
)
def test_level_of(policy: ToolPolicy, level: Level) -> None:
    assert level_of(policy) == level


def test_limits_hint() -> None:
    assert has_limits(ToolPolicy(enabled=True, read=True, modify=True, approval_eur=500))
    assert not has_limits(ToolPolicy(enabled=True, read=True, modify=True))


def test_member_entries() -> None:
    assert member_entry_for_level("allowed") is None
    assert member_entry_for_level("off") == {"enabled": False, "read": False, "modify": False}
    entry = member_entry_for_level("ask_first")
    assert entry is not None
    assert entry["approval_actions"] == ["modify"]
