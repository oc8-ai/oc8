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
    authorize_tool_call,
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


def offers_direct(
    view: DirectAccessView,
    *,
    connection_key: str,
    tool: str,
    scopes: Mapping[str, Any] | None,
) -> bool:
    """Whether a granted connection's tool may be offered at all -- the same
    rule `direct_access_decision` applies, without the per-call value."""
    right = required_right(tool, scopes)
    return view.present_member and any(
        policy.enabled and policy.has_right(right) and policy.offers(tool)
        for _department, policy in view.granted.get(connection_key, ())
    )


def direct_access_decision(
    view: DirectAccessView,
    *,
    connection_key: str | None,
    tool: str,
    scopes: Mapping[str, Any] | None,
    value: float | None,
    attributes: Mapping[str, Any],
    extra_thresholds: Sequence[float | None] = (),
) -> Decision:
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
    allowed = False
    held: Decision | None = None
    for department_id, policy in departments:
        verdict = authorize_tool_call(
            policies={connection_key: policy},
            connection_key=connection_key,
            right=right,
            tool=tool,
            value=value,
            attributes=attributes,
            extra_thresholds=extra_thresholds,
        )
        if verdict.effect is Effect.DENY:
            continue
        allowed = True
        if verdict.effect is Effect.REQUIRE_APPROVAL and held is None:
            held = Decision(
                Effect.REQUIRE_APPROVAL,
                verdict.reason,
                reason_code=verdict.reason_code,
                context={**verdict.context, HOLDING_DEPARTMENT: str(department_id)},
            )
    if not allowed:
        return Decision(
            Effect.DENY,
            f"the person you act for may not {right} through {connection_key!r}",
            reason_code=NOT_THIS_CONNECTION,
            context=terms,
        )
    return held if held is not None else Decision(Effect.ALLOW)
