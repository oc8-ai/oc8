"""B1 -- classify a tool call into a risk tier (spec §5 B1).

Pure classification only: callers apply posture and gates after B0 ALLOW.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from oc8.authz.pdp import required_right

RiskTier = Literal["read", "write", "destructive", "irreversible", "outward"]

_RANK: dict[RiskTier, int] = {
    "read": 0,
    "write": 1,
    "destructive": 2,
    "irreversible": 3,
    "outward": 4,
}

# Ruling 4: static tiers for control tools (not NeutralTool constructor edits).
_CONTROL_WRITE: frozenset[str] = frozenset(
    {
        "memory_write",
        "delegate_task",
        "write_output_file",
        "run_shell",
        "run_program",
        "propose_change",
        "decide_approval",
    }
)
_CONTROL_READ: frozenset[str] = frozenset(
    {
        "ask_user",
        "todo_write",
        "find_tools",
        "procedure_step_done",
        "search_knowledge",
        "fetch_url",
        "search_memory",
        "render_component",
        "request_decision",
        "read_reference_file",
        "read_instruction_file",
        "read_run_file",
        "list_pending_approvals",
        "department_status",
        "agent_status",
        "budget_overview",
        "kpi_overview",
    }
)

CONTROL_TOOL_TIERS: dict[str, RiskTier] = {
    **{name: "write" for name in _CONTROL_WRITE},
    **{name: "read" for name in _CONTROL_READ},
}


def _raise(current: RiskTier, floor: RiskTier) -> RiskTier:
    return floor if _RANK[floor] > _RANK[current] else current


def _base_tier(tool: str, scopes: Mapping[str, Any] | list[Any] | None) -> RiskTier:
    static = CONTROL_TOOL_TIERS.get(tool)
    if static is not None:
        return static
    return "read" if required_right(tool, scopes) == "read" else "write"


def classify_tier(
    tool: str,
    *,
    scopes: dict[str, Any] | list[Any] | None,
    config: dict[str, Any] | None,
    annotations: dict[str, Any] | None,
    arguments: dict[str, Any] | None = None,
) -> RiskTier:
    """Classify one tool call. Higher sources never lower a tier."""
    from oc8.agent.outward import is_outward_skipped

    tier = _base_tier(tool, scopes)

    if annotations:
        if annotations.get("destructiveHint"):
            tier = _raise(tier, "destructive")
        if (
            annotations.get("readOnlyHint")
            and tier == "write"
            and required_right(tool, scopes) == "read"
        ):
            tier = "read"

    cfg = config or {}
    if tool in (cfg.get("destructive_tools") or []):
        tier = _raise(tier, "destructive")
    if tool in (cfg.get("irreversible_tools") or []):
        tier = _raise(tier, "irreversible")
    if tool in (cfg.get("outward_tools") or []):
        skip = cfg.get("outward_skip_spec")
        skip_dict = skip if isinstance(skip, dict) else None
        if not is_outward_skipped(tool, arguments or {}, skip_dict):
            tier = _raise(tier, "outward")

    return tier
