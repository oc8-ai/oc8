"""The audit record of a tool call and its outcome (spec §5.7).

Every runtime -- the in-process engine, the isolated control plane and the MCP
gateway -- audits a call as `tool.call:<tool>` (the decision) and, when the call
was dispatched, as `tool.result:<tool>` (the outcome). Both name the run and,
for a chat run, the chat, so an admin can trace a call to where it came from.

The result event carries the outcome and the duration and nothing else: a tool
result can hold customer data or secrets, and the chain cannot be redacted
afterwards.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.audit.chain import append_event

logger = logging.getLogger(__name__)


def run_link(run_id: uuid.UUID | None, context: Mapping[str, Any] | None) -> dict[str, str]:
    """The `run_id` and, for a chat run, `chat_session_id` keys of a tool event.

    Empty for a call outside any run (a direct `run_agent` in a test)."""
    if run_id is None:
        return {}
    link = {"run_id": str(run_id)}
    session = (context or {}).get("chat_session_id")
    if session:
        link["chat_session_id"] = str(session)
    return link


async def audit_tool_result(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool: str,
    link: Mapping[str, str],
    connection: str | None,
    ok: bool,
    duration_ms: int,
    originating_operator: str | None,
    detail: Mapping[str, str | int] | None = None,
) -> None:
    """Stage the `tool.result:<tool>` event of a dispatched call.

    `detail` is what a core tool adds about its outcome (`ControlOutcome.
    result_detail`: a count, a provider name) -- never content. It cannot
    replace the keys set here.

    Never fails the call: a failure to stage it is logged and swallowed."""
    resource: dict[str, Any] = {**(detail or {}), "tool": tool, **link}
    if connection:
        resource["connection"] = connection
    resource["outcome"] = "ok" if ok else "error"
    resource["duration_ms"] = max(0, int(duration_ms))
    try:
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="agent",
            actor_id=agent_id,
            category="tool_action",
            action=f"tool.result:{tool}",
            resource=resource,
            decision="allow",
            originating_operator=originating_operator,
        )
    except Exception:
        logger.warning("could not audit the result of tool %s", tool, exc_info=True)
