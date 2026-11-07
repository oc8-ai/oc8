"""The four plain guardrail levels (D3a, design review D2).

Display and input only: a level maps onto existing ToolPolicy fields and is
never stored. Everything a level cannot express is a "limit" shown as a hint.
"""

from __future__ import annotations

from typing import Any, Literal

from oc8.authz.pdp import ToolPolicy

Level = Literal["off", "read_only", "ask_first", "allowed"]
LEVELS: tuple[Level, ...] = ("off", "read_only", "ask_first", "allowed")


def level_of(policy: ToolPolicy) -> Level:
    if not policy.enabled or not (policy.read or policy.modify):
        return "off"
    if not policy.modify:
        return "read_only"
    if "modify" in policy.approval_actions or "modify" in policy.member_approval_actions:
        return "ask_first"
    return "allowed"


def has_limits(policy: ToolPolicy) -> bool:
    """Details the four levels do not show: thresholds, single actions,
    conditions, a narrowed surface."""
    extra_actions = (policy.approval_actions | policy.member_approval_actions) - {"modify"}
    return bool(
        # Write without read: a modifying level assumes read too.
        (policy.modify and not policy.read)
        or policy.approval_eur is not None
        or policy.member_approval_eur is not None
        or extra_actions
        or policy.conditions
        or policy.member_conditions
        or policy.only is not None
    )


class LevelNotOffered(ValueError):
    """The picked level needs a right the base does not grant -- "read only"
    on a tool the department and agent only let the agent write with."""


def member_entry_for_level(level: Level, base: ToolPolicy) -> dict[str, Any] | None:
    """The member-term entry for a picked level on `base` (department ∩ agent);
    None removes the entry.

    Read and modify are capped by the base, so a level never asks for a right
    the base withholds: on a write-only base "ask first" stays write-only, and
    "read only" -- which would have to add read -- is not offered at all
    (`LevelNotOffered`)."""
    if level == "allowed":
        return None
    if level == "off":
        return {"enabled": False, "read": False, "modify": False}
    if level == "read_only":
        if not base.read:
            raise LevelNotOffered("read only is not offered: the agent cannot read with this tool")
        return {"enabled": True, "read": True, "modify": False}
    return {"enabled": True, "read": base.read, "modify": True, "approval_actions": ["modify"]}
