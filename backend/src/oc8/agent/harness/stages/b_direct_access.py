"""B0 for the Copilot's direct access (copilot direct access design §6).

Pure, like `b_authorize`. The runtime loads a `DirectAccessView` from the
database before EVERY call, so a revoked grant, a lost seat or a disconnected
system bites on the next call, not the next run.

A call through a grant is decided against the DEPARTMENTS the person sits in
whose frame enables the connection -- never against the Copilot's own
department. It passes when at least one of them grants the call's right. If
any of those that grant it hold the call for a human (`approval_eur`,
`approval_actions`, `conditions`), the call is held: a policy can only add a
gate (§1).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from oc8.authz.pdp import (
    NOT_THIS_CONNECTION,
    Decision,
    Effect,
    ToolPolicy,
    _frame_denial,
    authorize_tool_call,
    effective_tool_policies,
    required_right,
)

#: The Copilot's offer tool. Named here so the PEP can decide it without
#: importing the tool registry.
OFFER_DIRECT_ACCESS_TOOL: Final = "offer_direct_access"

#: `Decision.context` key: the department whose own policy holds a direct call.
#: Its approvers decide that call, not the person (a policy adds a gate).
HOLDING_DEPARTMENT: Final = "holding_department_id"


@dataclass(frozen=True)
class DirectAccessView:
    """What this Copilot run may reach right now, for the person it acts for."""

    #: The present member the run acts for; None when nobody is present.
    member_id: uuid.UUID | None = None
    #: tool key -> (department id, that department's frame policy for the key),
    #: only for keys with an active grant in this conversation that the member
    #: may still use. Ordered by department name.
    granted: Mapping[str, tuple[tuple[uuid.UUID, ToolPolicy], ...]] = field(default_factory=dict)

    @property
    def present_member(self) -> bool:
        return self.member_id is not None


NO_DIRECT_ACCESS: Final = DirectAccessView()


def _narrowed(
    policy: ToolPolicy, connection_key: str, narrowing: Mapping[str, Any] | None
) -> ToolPolicy:
    """The department's policy intersected with the Copilot's own pinned
    narrowing, by the one intersection every agent's frame goes through. A
    narrowing-only key never grants: only the department frames do."""
    if not narrowing:
        return policy
    frame = {"tools": {connection_key: policy.to_json()}}
    return effective_tool_policies(frame, dict(narrowing))[connection_key]


def offers_direct(
    view: DirectAccessView,
    *,
    connection_key: str,
    tool: str | None,
    scopes: Mapping[str, Any] | None,
    narrowing: Mapping[str, Any] | None = None,
) -> bool:
    """Whether a granted connection's tool may be offered at all -- the same
    rule `direct_access_decision` applies, without the per-call value.

    `tool=None` asks about the connection instead: may anything be READ
    through it. The right is "read"; a department's or the Copilot's `only`
    list does not refuse it unless it leaves no read tool (none of the
    `scopes`' read tools, or -- with no scopes to compare -- nothing at all)."""
    right = "read" if tool is None else required_right(tool, scopes)
    read_tools = _read_tools(scopes)
    return view.present_member and any(
        _frame_denial(
            {connection_key: policy},
            connection_key=connection_key,
            right=right,
            tool=tool,
        )
        is None
        and (tool is not None or _leaves_a_read(policy, read_tools))
        for _department, raw in view.granted.get(connection_key, ())
        for policy in (_narrowed(raw, connection_key, narrowing),)
    )


def _read_tools(scopes: Mapping[str, Any] | None) -> frozenset[str] | None:
    names = scopes.get("read") if scopes else None
    return frozenset(str(n) for n in names) if isinstance(names, (list, tuple)) else None


def _leaves_a_read(policy: ToolPolicy, read_tools: frozenset[str] | None) -> bool:
    if policy.only is None:
        return True
    return bool(policy.only & read_tools) if read_tools is not None else bool(policy.only)


_RANK = {Effect.ALLOW: 0, Effect.REQUIRE_APPROVAL: 1, Effect.DENY: 2}


def direct_access_decision(
    view: DirectAccessView,
    *,
    connection_key: str | None,
    tool: str,
    scopes: Mapping[str, Any] | None,
    value: float | None,
    attributes: Mapping[str, Any],
    extra_thresholds: Sequence[float | None] = (),
    narrowing: Mapping[str, Any] | None = None,
) -> Decision:
    """A department that does not grant the call is skipped; among those that
    do, the strictest verdict wins (DENY > REQUIRE_APPROVAL > ALLOW), so a
    policy only ever adds a gate (§1)."""
    right = required_right(tool, scopes)
    terms = {"connection": connection_key, "tool": tool, "right": right}
    departments = view.granted.get(connection_key, ()) if connection_key else ()
    if connection_key is None or not view.present_member or not departments:
        return Decision(
            Effect.DENY,
            f"no direct access to {connection_key!r} in this conversation -- "
            "offer it with offer_direct_access first",
            reason_code=NOT_THIS_CONNECTION,
            context=terms,
        )
    #: (department id, its own verdict, the verdict with the extra thresholds)
    granting: list[tuple[uuid.UUID, Decision, Decision]] = []
    for department_id, raw in departments:
        policy = _narrowed(raw, connection_key, narrowing)
        policies = {connection_key: policy}
        if (
            _frame_denial(policies, connection_key=connection_key, right=right, tool=tool)
            is not None
        ):
            continue
        common: dict[str, Any] = {
            "policies": policies,
            "connection_key": connection_key,
            "right": right,
            "tool": tool,
            "value": value,
            "attributes": attributes,
        }
        # Attribution: only the department's RAW policy may name it as the
        # holder, never a hold the Copilot's narrowing or a threshold caused.
        own = authorize_tool_call(**{**common, "policies": {connection_key: raw}})
        full = authorize_tool_call(**common, extra_thresholds=extra_thresholds)
        granting.append((department_id, own, full))
    if not granting:
        return Decision(
            Effect.DENY,
            f"the person you act for may not {right} through {connection_key!r}",
            reason_code=NOT_THIS_CONNECTION,
            context=terms,
        )
    strictest = max(_RANK[full.effect] for _d, _o, full in granting)
    tied = [(d, o, f) for d, o, f in granting if _RANK[f.effect] == strictest]
    if strictest == _RANK[Effect.ALLOW]:
        return Decision(Effect.ALLOW)
    if strictest == _RANK[Effect.DENY]:
        return tied[0][2]
    # Held: name a department only when its OWN policy produced the hold; a hold
    # that only the Copilot's / a skill's threshold caused is the person's.
    for department_id, own, full in tied:
        if own.effect is Effect.REQUIRE_APPROVAL:
            return Decision(
                Effect.REQUIRE_APPROVAL,
                full.reason,
                reason_code=full.reason_code,
                context={**full.context, HOLDING_DEPARTMENT: str(department_id)},
            )
    return tied[0][2]
