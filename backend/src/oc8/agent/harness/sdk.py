"""Generate the oc8_tools.py module for code-mode runs (spec §8).

Pure string generator — no network, no side effects. The isolated shell writes
the result to /workspace/oc8_tools.py; every function still POSTs to /tool.
"""

from __future__ import annotations

import textwrap
from typing import Any

from oc8.modelrouter.types import NeutralTool

_HEADER = textwrap.dedent(
    '''\
    """Auto-generated OC8 tools SDK. Do not edit."""

    from __future__ import annotations

    import os
    import uuid

    import httpx


    class ToolDenied(Exception):
        def __init__(self, reason: str) -> None:
            self.reason = reason
            super().__init__(reason)


    class ApprovalRequired(Exception):
        def __init__(self, tool: str, args: dict, reason: str) -> None:
            self.tool = tool
            self.args = args
            self.reason = reason
            super().__init__(reason)


    def _call_tool(name: str, arguments: dict) -> str:
        base = os.environ["OC8_INTERNAL_URL"].rstrip("/")
        token = os.environ["OC8_AGENT_TOKEN"]
        run_id = os.environ["OC8_RUN_ID"]
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{base}/api/v1/internal/agent/{run_id}/tool"
        body = {"id": str(uuid.uuid4()), "name": name, "arguments": arguments}
        with httpx.Client(timeout=300.0, headers=headers) as client:
            resp = client.post(url, json=body)
            resp.raise_for_status()
            result = resp.json()
        status = result.get("status", "")
        output = result.get("output", "")
        if status == "denied" or (isinstance(output, str) and output.startswith("ERROR:")):
            raise ToolDenied(output or status)
        if status in ("waiting_for_approval", "waiting_for_input"):
            raise ApprovalRequired(name, arguments, output)
        return output

    '''
)

_FIND_TOOLS = textwrap.dedent(
    '''\

    def find_tools(query: str) -> str:
        """Search the deferred tool catalog."""
        return _call_tool("find_tools", {"query": query})
    '''
)


def _docstring(description: str) -> str:
    escaped = description.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'    """{escaped}"""'


def _param_annotation(schema: dict[str, Any]) -> str:
    param_type = schema.get("type")
    if param_type == "string":
        return "str"
    if param_type == "integer":
        return "int"
    if param_type == "number":
        return "float"
    if param_type == "boolean":
        return "bool"
    if param_type == "array":
        return "list"
    if param_type == "object":
        return "dict"
    return "Any"


def _render_function(tool: NeutralTool) -> str:
    properties = tool.parameters.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    params = [name for name in properties if name.isidentifier()]
    params.sort()
    if params:
        sig = ", ".join(f"{name}: {_param_annotation(properties[name])}" for name in params)
        signature = f"def {tool.name}(*, {sig}) -> str:"
    else:
        signature = f"def {tool.name}() -> str:"
    arg_dict = ", ".join(f'"{name}": {name}' for name in params)
    body = [
        signature,
        _docstring(tool.description),
        f'    return _call_tool("{tool.name}", {{{arg_dict}}})',
        "",
    ]
    return "\n".join(body)


def render_oc8_tools(tools: list[NeutralTool]) -> str:
    """Return deterministic Python source for an oc8_tools module."""
    parts = [_HEADER]
    for tool in sorted(tools, key=lambda t: t.name):
        if tool.name == "find_tools":
            continue
        if not tool.name.isidentifier():
            continue
        parts.append(_render_function(tool))
    parts.append(_FIND_TOOLS)
    return "\n".join(parts)
