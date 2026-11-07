from __future__ import annotations

import pytest

from oc8.authz.levels import (
    Level,
    LevelNotOffered,
    has_limits,
    level_of,
    member_entry_for_level,
)
from oc8.authz.pdp import ToolPolicy, member_term_violations

FULL = ToolPolicy(enabled=True, read=True, modify=True)
WRITE_ONLY = ToolPolicy(enabled=True, read=False, modify=True)


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
    assert has_limits(ToolPolicy(enabled=True, read=False, modify=True))


def test_member_entries() -> None:
    assert member_entry_for_level("allowed", FULL) is None
    assert member_entry_for_level("off", FULL) == {
        "enabled": False,
        "read": False,
        "modify": False,
    }
    entry = member_entry_for_level("ask_first", FULL)
    assert entry is not None
    assert entry["approval_actions"] == ["modify"]
    assert entry["read"] is True


def test_member_entries_on_a_write_only_base_never_add_read() -> None:
    entry = member_entry_for_level("ask_first", WRITE_ONLY)
    assert entry == {
        "enabled": True,
        "read": False,
        "modify": True,
        "approval_actions": ["modify"],
    }
    assert member_term_violations({"mail": WRITE_ONLY}, {"tools": {"mail": entry}}) == []
    off = member_entry_for_level("off", WRITE_ONLY)
    assert member_term_violations({"mail": WRITE_ONLY}, {"tools": {"mail": off}}) == []
    with pytest.raises(LevelNotOffered):
        member_entry_for_level("read_only", WRITE_ONLY)
