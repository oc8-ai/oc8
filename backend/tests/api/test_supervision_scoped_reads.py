"""Pin the supervision router's direct agent load for review.

The endpoint resolves exactly the agent named in its path before changing that
agent's supervisor; it is not a department browse.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTER = Path(__file__).resolve().parents[2] / "src/oc8/api/v1/supervision.py"


def test_supervision_router_only_loads_the_path_agent_for_supervisor_update() -> None:
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"), filename=str(ROUTER))
    agent_gets = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "Agent"
    ]

    assert len(agent_gets) == 1
    assert isinstance(agent_gets[0].args[1], ast.Name)
    assert agent_gets[0].args[1].id == "agent_id"
