"""Proof that the PDP is actor-neutral: Task C1.

The agent path (`agent/engine.py`) builds `effective_tool_policies(frame,
narrowing)` once per run and calls `authorize_tool_call` for every tool call.
`oc8.coding` already runs a sandbox through that exact same path as a synthetic
connection (`CODING_FRAME_KEY`) rather than a special case, which is the
existing evidence that the agent-token 403 lives only in
`api/mcp_gateway.py::_caller`, not in the PDP itself.

`ToolActor`/`decide_for_actor` (`oc8.authz.actor`) name that requirement so a
future non-agent caller -- a workflow node, a room -- can be governed the same
way without a second, weaker door. This file is the executable form of "there
is no ungoverned side door": for a representative set of frame/narrowing/
tool/right/value/attribute combinations, the actor path and the agent path
must produce byte-identical `Decision`s -- not just an identical `effect`,
which two paths that happened to agree on the headline result but diverged
on `reason`/`reason_code`/`context` would also satisfy. If they ever
diverge, a workflow can do something an agent cannot.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any
from uuid import uuid4

import pytest

from oc8.authz.actor import ToolActor, decide_for_actor
from oc8.authz.pdp import authorize_tool_call, effective_tool_policies

# (frame, narrowing, connection, tool, right, value, attributes)
CASES: list[
    tuple[dict[str, Any], dict[str, Any], str, str, str, float | None, dict[str, Any] | None]
] = [
    # plain read allow
    (
        {"tools": {"odoo": {"enabled": True, "read": True}}},
        {},
        "odoo",
        "search_read",
        "read",
        None,
        None,
    ),
    # plain modify allow
    (
        {"tools": {"odoo": {"enabled": True, "modify": True}}},
        {},
        "odoo",
        "write",
        "modify",
        None,
        None,
    ),
    # over the euro threshold -> require_approval
    (
        {"tools": {"odoo": {"enabled": True, "modify": True, "approval_eur": 3000}}},
        {},
        "odoo",
        "create_order",
        "modify",
        4200.0,
        None,
    ),
    # under the euro threshold -> allow
    (
        {"tools": {"odoo": {"enabled": True, "modify": True, "approval_eur": 3000}}},
        {},
        "odoo",
        "create_order",
        "modify",
        100.0,
        None,
    ),
    # narrowing tightens a frame-granted right away -> deny for "right not
    # granted", distinct from the "connection disabled" deny below. The
    # narrowing must explicitly re-assert enabled=True: leaving it unset
    # would make `effective_tool_policies` compute enabled=False (frame AND
    # narrowing, and narrowing's own default is False) and this would
    # silently collapse into the same "disabled" branch as the next case,
    # never touching the one it's meant to exercise.
    (
        {"tools": {"odoo": {"enabled": True, "modify": True}}},
        {"tools": {"odoo": {"enabled": True, "modify": False}}},
        "odoo",
        "write",
        "modify",
        None,
        None,
    ),
    # connection not granted by the frame at all -> deny
    ({"tools": {}}, {}, "odoo", "search_read", "read", None, None),
    # approval_actions: always requires a human, whatever the value
    (
        {"tools": {"odoo": {"enabled": True, "modify": True, "approval_actions": ["modify"]}}},
        {},
        "odoo",
        "write",
        "modify",
        0.0,
        None,
    ),
    # approval_actions naming a specific tool rather than a right
    (
        {
            "tools": {
                "odoo": {
                    "enabled": True,
                    "modify": True,
                    "approval_actions": ["mail_send"],
                }
            }
        },
        {},
        "odoo",
        "mail_send",
        "modify",
        None,
        None,
    ),
    # `only` intersection: tool outside the surface -> deny
    (
        {
            "tools": {
                "odoo": {
                    "enabled": True,
                    "read": True,
                    "only": ["res_partner_read"],
                }
            }
        },
        {},
        "odoo",
        "crm_lead_read",
        "read",
        None,
        None,
    ),
    # `only` intersection: tool inside the surface -> allow
    (
        {
            "tools": {
                "odoo": {
                    "enabled": True,
                    "read": True,
                    "only": ["res_partner_read"],
                }
            }
        },
        {},
        "odoo",
        "res_partner_read",
        "read",
        None,
        None,
    ),
    # generic condition matches -> require_approval. `attributes` must
    # actually be passed on both sides, or `_condition_matches` sees an
    # attribute-less call and this collapses into plain ALLOW without ever
    # reaching `evaluate_conditions`'s match branch.
    (
        {
            "tools": {
                "odoo": {
                    "enabled": True,
                    "modify": True,
                    "conditions": [
                        {
                            "attribute": "order_value",
                            "datatype": "number",
                            "operator": ">",
                            "value": 5000,
                            "then": "require_approval",
                        }
                    ],
                }
            }
        },
        {},
        "odoo",
        "create_order",
        "modify",
        12480.0,
        {"order_value": 12480},
    ),
    # generic condition evaluated but doesn't match -> falls through to
    # allow. `attributes` is passed here too (rather than omitted), so this
    # genuinely exercises "the condition was checked and failed", not just
    # "the attribute was never supplied".
    (
        {
            "tools": {
                "odoo": {
                    "enabled": True,
                    "modify": True,
                    "conditions": [
                        {
                            "attribute": "order_value",
                            "datatype": "number",
                            "operator": ">",
                            "value": 5000,
                            "then": "require_approval",
                        }
                    ],
                }
            }
        },
        {},
        "odoo",
        "create_order",
        "modify",
        100.0,
        {"order_value": 100},
    ),
    # tool not enabled at all -> deny
    (
        {"tools": {"odoo": {"enabled": False, "read": True}}},
        {},
        "odoo",
        "search_read",
        "read",
        None,
        None,
    ),
]


@pytest.mark.parametrize("frame,narrowing,conn,tool,right,value,attributes", CASES)
def test_actor_path_and_agent_path_agree(
    frame: dict[str, Any],
    narrowing: dict[str, Any],
    conn: str,
    tool: str,
    right: str,
    value: float | None,
    attributes: dict[str, Any] | None,
) -> None:
    """The executable form of 'there is no ungoverned side door'.

    Asserts full `Decision` equality (`effect`, `reason`, `reason_code`,
    `context` all included via dataclass `__eq__`) rather than picking two
    fields to compare -- on the DENY cases here `reason_code` is `None` on
    both sides regardless of what either function actually did, so comparing
    only that field would never catch the two paths disagreeing on
    `reason`/`context`, or on `effect` itself if `reason_code` merely
    happened to still line up.

    If these two ever diverge, a workflow can do something an agent cannot,
    which is exactly the failure the workflow design exists to prevent.
    """
    agent_decision = authorize_tool_call(
        policies=effective_tool_policies(frame, narrowing),
        connection_key=conn,
        right=right,
        value=value,
        tool=tool,
        attributes=attributes,
    )
    actor = ToolActor(
        tenant_id=uuid4(),
        department_id=uuid4(),
        frame=frame,
        narrowing=narrowing,
        label="workflow:test",
    )
    actor_decision = decide_for_actor(
        actor, connection_key=conn, tool=tool, right=right, value=value, attributes=attributes
    )
    assert actor_decision == agent_decision


def test_tool_actor_is_frozen_and_carries_attribution() -> None:
    """`ToolActor` names the (frame, narrowing, department, label) tuple the PDP
    actually needs -- not an `Agent`. A workflow node or a room can build one
    without ever touching the `Agent` table."""
    actor = ToolActor(
        tenant_id=uuid4(),
        department_id=uuid4(),
        frame={},
        narrowing={},
        label="workflow:abc123",
    )
    assert actor.label == "workflow:abc123"
    with pytest.raises(FrozenInstanceError):
        actor.label = "mutated"  # type: ignore[misc]
