"""Mock Jira MCP server: issue create/get/update/search, file-backed state.
Launch: `python -m oc8_evals.mocks.jira` with OC8_EVAL_STATE_FILE."""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types

from oc8_evals.mocks._server import Handler, serve
from oc8_evals.mocks._store import Store

TOOLS: list[types.Tool] = [
    types.Tool(
        name="jira_create_issue",
        description="Create a new issue.",
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["summary", "description"],
        },
    ),
    types.Tool(
        name="jira_get_issue",
        description="Get one issue by key.",
        inputSchema={
            "type": "object",
            "properties": {"issue_key": {"type": "string"}},
            "required": ["issue_key"],
        },
    ),
    types.Tool(
        name="jira_update_issue",
        description="Update an issue's description.",
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["issue_key", "description"],
        },
    ),
    types.Tool(
        name="jira_search",
        description="Search issues whose summary or description contains a query.",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
]


def handlers(store: Store) -> dict[str, Handler]:
    async def jira_create_issue(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            key = store.next_id(state, "ISSUE")
            state.setdefault("issues", []).append(
                {
                    "key": key,
                    "summary": str(args["summary"]),
                    "description": str(args["description"]),
                }
            )
            return key

        return store.update(_do)

    async def jira_get_issue(args: dict[str, Any]) -> str:
        for issue in store.read().get("issues", []):
            if issue["key"] == args["issue_key"]:
                return json.dumps(issue)
        return "ERROR: unknown issue"

    async def jira_update_issue(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            for issue in state.get("issues", []):
                if issue["key"] == args["issue_key"]:
                    issue["description"] = str(args["description"])
                    return f"updated {issue['key']}"
            return "ERROR: unknown issue"

        return store.update(_do)

    async def jira_search(args: dict[str, Any]) -> str:
        q = str(args["query"]).lower()
        return json.dumps(
            [
                issue
                for issue in store.read().get("issues", [])
                if q in issue["summary"].lower() or q in issue["description"].lower()
            ]
        )

    return {
        "jira_create_issue": jira_create_issue,
        "jira_get_issue": jira_get_issue,
        "jira_update_issue": jira_update_issue,
        "jira_search": jira_search,
    }


if __name__ == "__main__":
    serve("jira", TOOLS, handlers(Store.from_env()))
