"""The actor-neutral seam over the PDP (tech-spec §5.3).

`agent/engine.py` resolves `effective_tool_policies(frame, narrowing)` once per
run and calls `authorize_tool_call` for every tool call; `oc8.coding` already
runs a sandbox through that identical path as a synthetic connection rather
than a special case. This module names what both of those actually needed --
a (frame, narrowing) pair and a department -- so a future non-agent caller (a
workflow node, a room) can be governed by the same decision, not a second one
that happens to agree with it today.

`decide_for_actor` is deliberately a near-trivial wrapper: it must never grow
its own logic, or the parity it exists to prove would be manufactured rather
than demonstrated. See `tests/authz/test_actor_neutral_authorization.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from oc8.authz.pdp import Decision, authorize_tool_call, effective_tool_policies


@dataclass(frozen=True)
class ToolActor:
    """Anything that may call a tool: an agent, a workflow node, a room.

    Deliberately not an `Agent` -- the PDP only ever needed a (frame,
    narrowing) pair and a department. Naming that requirement is the whole
    change.
    """

    tenant_id: uuid.UUID
    department_id: uuid.UUID
    frame: dict[str, Any]
    narrowing: dict[str, Any]
    label: str  # audit/approval attribution, e.g. "agent:<id>" | "workflow:<id>"


def decide_for_actor(
    actor: ToolActor,
    *,
    connection_key: str | None,
    tool: str,
    right: str,
    value: float | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Decision:
    """The same decision `agent/engine.py` reaches, for any `ToolActor`.

    Resolves `effective_tool_policies` from the actor's own frame/narrowing
    and calls `authorize_tool_call` -- the live gate every real tool call
    already goes through. No agent-shaped branching here on purpose.
    """
    policies = effective_tool_policies(actor.frame, actor.narrowing)
    return authorize_tool_call(
        policies=policies,
        connection_key=connection_key,
        right=right,
        value=value,
        tool=tool,
        attributes=attributes,
    )
