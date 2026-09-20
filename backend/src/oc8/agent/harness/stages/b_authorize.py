"""B0 -- the policy enforcement point for one tool call (spec §5 B0).

Moved out of engine.py unchanged. Deliberately pure: checks needing the DB
(does a delegation target exist, is it in this department) live in the
control-tool dispatcher.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from oc8 import models as m
from oc8.agent.control_tools import DEPTH_LIMIT_REASON, MAX_DELEGATION_DEPTH
from oc8.agent.tool_semantics import extract_attributes, extract_value
from oc8.authz.pdp import Decision, Effect, ToolPolicy, authorize_tool_call, required_right
from oc8.memory.policy import authorize_memory_write
from oc8.memory.router import MAX_MEMORY_CONTENT_LENGTH
from oc8.modelrouter import ToolCall


def authorize(
    agent: m.Agent,
    tc: ToolCall,
    *,
    frame: dict[str, Any],
    delegation_depth: int = 0,
    tool_policies: Mapping[str, ToolPolicy],
    connection_key: str | None,
    tool_scopes: Mapping[str, Any] | None,
    skill_thresholds: Sequence[float | None] = (),
    skill_tool_names: frozenset[str] = frozenset(),
    value_spec: dict[str, Any] | None = None,
    guardrail_attribute_specs: Sequence[dict[str, Any]] = (),
) -> Decision:
    """PEP for a tool call. Every connection tool is decided against the
    department frame (§5.3): which entry governs it is the connection key, and
    which right it needs comes from the connection's `scopes` (unclassified ==
    write, fail-closed). memory_write is gated by the §10 tier policy instead;
    delegate_task (§7) is ALLOW/DENY only -- a delegation carries no monetary
    value. Checks needing the DB (does the target exist, is it in this
    department) live in _delegate, since this function is deliberately pure.

    The frame is bypassed only for tool names that are actually assigned
    skill-invocation tools (`skill_tool_names`) -- never by a `skill_`
    name-prefix match, since MCP tool names flow in unsanitized from a remote
    server and a connection could name a plain tool `skill_anything` to dodge
    the frame check entirely. A stray `skill_`-prefixed tool that isn't one of
    this agent's assigned skills falls through to the normal frame check
    below, exactly like any other tool of that connection."""
    if tc.name in skill_tool_names:
        return Decision(Effect.ALLOW)
    if tc.name == "ask_user":
        return Decision(Effect.ALLOW)
    if tc.name == "propose_change":
        # Like ask_user: it belongs to no connection, so the department frame
        # has nothing to decide it against -- the Assistant's chat run has no
        # tool connection bound at all, and falling through would DENY. That
        # DENY is not enforced (execute_control_tool dispatches control tools
        # before the deny branch and this one never reads `decision`), it is
        # only WRITTEN, so every successful call would be audited as a denial.
        # Deliberate consequence: `is_tenant_assistant`, checked in the
        # dispatch, is then the only gate on this tool -- which is what it
        # should be for a tool that can only ever produce a draft a human has
        # to approve before anything changes.
        return Decision(Effect.ALLOW)
    if tc.name == "decide_approval":
        # Like propose_change and ask_user: it belongs to no connection, so
        # the department frame has nothing to decide it against. Real
        # authorisation for a decision happens where it must, inside
        # `decide_approval` (approvals/service.py) via `_may_apply_the_effect`
        # and `_resolve_agent_actor`'s scope -- this ALLOW only keeps a
        # successful call from being audited as a denial for a tool that was
        # never going to be enforced by this frame in the first place.
        return Decision(Effect.ALLOW)
    if tc.name == "run_shell":
        # Like propose_change and decide_approval, immediately above: it
        # belongs to no connection, so the department frame has nothing to
        # decide it against, and execute_control_tool never reads this
        # decision for run_shell either (see RUN_SHELL's dispatch in
        # control_tools.py, which only checks for a local_result) -- without
        # this special case, a run with no MCP connection bound (only the
        # builtin isolated shell offers run_shell at all -- see
        # internal_agent.py's offer_run_shell) would have every successful
        # call audited as a denial.
        return Decision(Effect.ALLOW)
    if tc.name == "delegate_task":
        if not agent.is_team_lead:
            return Decision(Effect.DENY, "only a team lead can delegate tasks")
        if not str(tc.arguments.get("task_text", "")).strip():
            return Decision(Effect.DENY, "task_text must not be empty")
        raw_target = str(tc.arguments.get("agent_id", ""))
        try:
            target_id = uuid.UUID(raw_target)
        except ValueError:
            return Decision(Effect.DENY, f"invalid agent_id: {raw_target!r}")
        if target_id == agent.id:
            return Decision(Effect.DENY, "an agent cannot delegate to itself")
        if delegation_depth + 1 > MAX_DELEGATION_DEPTH:
            return Decision(Effect.DENY, DEPTH_LIMIT_REASON)
        return Decision(Effect.ALLOW)
    if tc.name == "memory_write":
        content = str(tc.arguments.get("content", ""))
        tier = str(tc.arguments.get("tier", ""))
        if not content.strip():
            return Decision(Effect.DENY, "content must not be empty")
        if len(content) > MAX_MEMORY_CONTENT_LENGTH:
            return Decision(Effect.DENY, f"content exceeds {MAX_MEMORY_CONTENT_LENGTH} characters")
        return authorize_memory_write(frame, agent.narrowing or {}, tier)
    agent_threshold = (agent.presentation or {}).get("approval_value_eur")
    applicable_attributes = [
        spec
        for spec in guardrail_attribute_specs
        if not spec.get("tools") or tc.name in spec["tools"]
    ]
    return authorize_tool_call(
        policies=tool_policies,
        connection_key=connection_key,
        right=required_right(tc.name, tool_scopes),
        tool=tc.name,
        value=extract_value(tc.arguments, value_spec),
        attributes=extract_attributes(tc.arguments, applicable_attributes),
        extra_thresholds=(
            float(agent_threshold) if agent_threshold is not None else None,
            *skill_thresholds,
        ),
    )
