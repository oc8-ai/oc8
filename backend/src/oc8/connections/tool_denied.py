"""A tool call the frame withholds, told to the person and to the model.

`authorize_tool_call` names its four frame denials (`Decision.reason_code`).
Without more, the model got the PDP's internal sentence ("'microsoft365' does
not grant the tool 'mail_search' in this frame") and reported a vague
technical blocker; nobody learnt that an admin allows the tool in the
department's guardrails. Every runtime's DENY branch therefore asks
`tool_denied` for a description: recorded on the run (`tool_denied`, shown
like `connection_required`) and turned into one sentence for the model.

Non-secret on purpose: connection key, tool name, right, the denial code and
where it is fixed -- never arguments or credentials.

Recording only. The decision itself is untouched: this runs after the DENY.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import Decision, Effect, authorize_tool_call, effective_tool_policies
from oc8.connections.integrations import integration_for_connection
from oc8.runtime.run_context import merge_context

#: The DENY codes of `authorize_tool_call` that a frame or a narrowing causes.
FRAME_DENIALS: Final = frozenset(
    {"connection_not_in_frame", "connection_disabled", "right_not_granted", "tool_not_in_frame"}
)


#: How much of a model-supplied name is echoed back (to the model, onto the
#: run, into the UI): the name is whatever the model asked for.
MAX_ECHOED_NAME: Final = 80


def _cap(name: str) -> str:
    return name if len(name) <= MAX_ECHOED_NAME else name[: MAX_ECHOED_NAME - 1] + "…"


@dataclass(frozen=True)
class ToolDenied:
    connection: str
    tool: str | None
    right: str | None
    #: One of FRAME_DENIALS.
    reason: str
    #: Where it is allowed: the department's guardrails, or -- the frame
    #: grants it and only the agent's own narrowing withholds it -- the
    #: agent's.
    scope: Literal["department", "agent"]
    department_id: str | None
    agent_id: str | None
    #: The integration's display name ("Microsoft 365"), when the connection
    #: belongs to a plugin on disk.
    label: str | None = None

    def as_context(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "tool": self.tool,
            "label": self.label,
            "right": self.right,
            "reason": self.reason,
            "scope": self.scope,
            "departmentId": self.department_id,
            "agentId": self.agent_id,
        }

    def sentence(self) -> str:
        """What the model is told: one sentence it can pass on. The
        harness's shaping puts its own `ERROR from oc8 (<tool>):` in front."""
        system = f" ({self.label})" if self.label else ""
        what = (
            f"the tool {self.tool!r}{system}"
            if self.tool
            else f"the connection {self.connection!r}{system}"
        )
        if self.scope == "agent":
            return (
                f"this agent's guardrails do not allow {what}, so do not retry it; "
                "tell the user that someone who manages this agent can enable it in the "
                "agent's guardrails."
            )
        return (
            f"this department does not allow {what}, so do not retry it; "
            "tell the user that an administrator can enable it in the department's guardrails."
        )

    def model_message(self) -> str:
        """The tool result where nothing shapes it (the MCP gateway)."""
        return f"ERROR: {self.sentence()}"


def tool_denied(
    decision: Decision,
    *,
    frame: Mapping[str, Any] | None,
    agent: m.Agent,
    conn: m.McpConnection | None = None,
) -> ToolDenied | None:
    """The description of a frame denial; None for any other decision.

    `frame` is the department frame the call was decided against (with the
    agent's narrowing): asking the frame alone the same question tells whose
    guardrails withhold it."""
    if decision.effect is not Effect.DENY or decision.reason_code not in FRAME_DENIALS:
        return None
    terms = decision.context
    # read_resource takes its connection from the call's arguments: capped too.
    connection = _cap(str(terms.get("connection") or ""))
    if not connection:
        return None
    tool = terms.get("tool")
    right = terms.get("right")
    by_frame = authorize_tool_call(
        policies=effective_tool_policies(dict(frame or {}), {}),
        connection_key=connection,
        right=str(right or "read"),
        value=None,
        tool=str(tool) if tool else None,
    )
    # An agent without a department has no department guardrails to send
    # anyone to: only its own.
    scope: Literal["department", "agent"] = (
        "department"
        if agent.department_id is not None and by_frame.reason_code in FRAME_DENIALS
        else "agent"
    )
    integration = integration_for_connection(conn) if conn is not None else None
    return ToolDenied(
        connection=connection,
        tool=_cap(str(tool)) if tool else None,
        right=str(right) if right else None,
        reason=str(decision.reason_code),
        scope=scope,
        department_id=str(agent.department_id) if agent.department_id else None,
        agent_id=str(agent.id) if agent.id else None,
        label=integration.label if integration is not None else None,
    )


async def record_tool_denied(
    db: AsyncSession,
    run: m.AgentRun,
    denied: ToolDenied,
    *,
    ctx: dict[str, Any] | None = None,
) -> None:
    """Put it on the run (one statement, keeping every other key). `ctx`: a
    caller's in-request copy of the context it writes back whole -- the
    isolated runtime's /tool -- gets it too, as `run_mcp_env` does."""
    patch = {"tool_denied": denied.as_context()}
    await merge_context(db, run, patch)
    if ctx is not None:
        ctx.update(patch)
