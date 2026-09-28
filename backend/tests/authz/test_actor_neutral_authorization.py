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
tool/right/value combinations, the actor path and the agent path must produce
byte-identical decisions. If they ever diverge, a workflow can do something an
agent cannot.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any
from uuid import uuid4

import pytest

from oc8.authz.actor import ToolActor, decide_for_actor
from oc8.authz.pdp import authorize_tool_call, effective_tool_policies

# (frame, narrowing, connection, tool, right, value)
CASES: list[tuple[dict[str, Any], dict[str, Any], str, str, str, float | None]] = [
    # plain read allow
    ({"tools": {"odoo": {"enabled": True, "read": True}}}, {}, "odoo", "search_read", "read", None),
    # plain modify allow
    ({"tools": {"odoo": {"enabled": True, "modify": True}}}, {}, "odoo", "write", "modify", None),
    # over the euro threshold -> require_approval
    (
        {"tools": {"odoo": {"enabled": True, "modify": True, "approval_eur": 3000}}},
        {},
        "odoo",
        "create_order",
        "modify",
        4200.0,
    ),
    # under the euro threshold -> allow
    (
        {"tools": {"odoo": {"enabled": True, "modify": True, "approval_eur": 3000}}},
        {},
        "odoo",
        "create_order",
        "modify",
        100.0,
    ),
    # narrowing tightens a frame-granted right away -> deny
    (
        {"tools": {"odoo": {"enabled": True, "modify": True}}},
        {"tools": {"odoo": {"modify": False}}},
        "odoo",
        "write",
        "modify",
        None,
    ),
    # connection not granted by the frame at all -> deny
    ({"tools": {}}, {}, "odoo", "search_read", "read", None),
    # approval_actions: always requires a human, whatever the value
    (
        {"tools": {"odoo": {"enabled": True, "modify": True, "approval_actions": ["modify"]}}},
        {},
        "odoo",
        "write",
        "modify",
        0.0,
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
    ),
    # generic condition matches -> require_approval
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
    ),
    # generic condition present but doesn't match -> falls through to allow
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
    ),
    # tool not enabled at all -> deny
    (
        {"tools": {"odoo": {"enabled": False, "read": True}}},
        {},
        "odoo",
        "search_read",
        "read",
        None,
    ),
]


@pytest.mark.parametrize("frame,narrowing,conn,tool,right,value", CASES)
def test_actor_path_and_agent_path_agree(
    frame: dict[str, Any],
    narrowing: dict[str, Any],
    conn: str,
    tool: str,
    right: str,
    value: float | None,
) -> None:
    """The executable form of 'there is no ungoverned side door'.

    If these two ever diverge, a workflow can do something an agent cannot,
    which is exactly the failure the workflow design exists to prevent.
    """
    agent_decision = authorize_tool_call(
        policies=effective_tool_policies(frame, narrowing),
        connection_key=conn,
        right=right,
        value=value,
        tool=tool,
    )
    actor = ToolActor(
        tenant_id=uuid4(),
        department_id=uuid4(),
        frame=frame,
        narrowing=narrowing,
        label="workflow:test",
    )
    actor_decision = decide_for_actor(
        actor, connection_key=conn, tool=tool, right=right, value=value
    )
    assert actor_decision.effect is agent_decision.effect
    assert actor_decision.reason_code == agent_decision.reason_code


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
