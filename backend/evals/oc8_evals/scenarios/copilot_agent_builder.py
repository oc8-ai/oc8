"""A member asks the Copilot for an invoice checker (Copilot Agent Builder §6).

Pass: a valid builder proposal (kind agent_builder, draft, first step
agent.create) for the member's chat within at most 3 ask_user rounds, and no
write or connection call before the proposal. Scored on DB state and the run's
own tool log, never on the transcript's wording.

NOT YET RUNNABLE, same reason as copilot_keep_track: the harness cannot start a
member's Copilot chat turn (it starts runs on a fixture agent straight in the
DB). `setup` fails loudly; the scoring below is exercised by
`evals/tests/test_agent_builder_scenario.py` and works unchanged once `setup`
returns `{"session_id": ..., "started_at": ..., "connection_scopes": ...}`
(`connection_scopes` is optional: the agent's connection `scopes`, e.g.
`{"read": [...], "modify": [...]}`; without it every connection call is
classified as a write, fail closed).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from oc8.agent.control_tools import CONTROL_TOOL_NAMES
from oc8.authz.pdp import required_right
from oc8.chat.modes import AGENT, WRITING_CONTROL_TOOLS
from oc8.copilot.models import CopilotOperation, CopilotProposal
from oc8.db.session import tenant_session
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext

MAX_ROUNDS = 3


def question_rounds(tool_calls: list[dict[str, Any]]) -> int:
    return sum(1 for call in tool_calls if call.get("tool") == "ask_user")


def writes_or_connection_calls_before_proposal(
    tool_calls: list[dict[str, Any]],
    connection_scopes: Mapping[str, Any] | None = None,
) -> list[str]:
    """Tools called before the first propose_agent that change something.

    Counts a writing core tool the mode does not keep (`WRITING_CONTROL_TOOLS`
    minus `AGENT.permitted_writes`) and a connection tool whose scope is not
    read. Read-only connection calls, `propose_agent` itself and anything after
    it do not count.
    """
    found: list[str] = []
    for call in tool_calls:
        name = str(call.get("tool"))
        if name == "propose_agent":
            break
        if name in CONTROL_TOOL_NAMES:
            if name in WRITING_CONTROL_TOOLS and name not in AGENT.permitted_writes:
                found.append(name)
        elif required_right(name, connection_scopes) != "read":
            found.append(name)
    return found


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    raise RuntimeError(
        "copilot_agent_builder is not runnable yet: the eval harness cannot start a "
        "Copilot chat turn for a member (see the module docstring)"
    )


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    since: dt.datetime = seeded["started_at"]
    calls = list(ctx.run_context.get("toolCalls", []))
    async with tenant_session(ctx.tenant_id) as db:
        proposals = (
            (
                await db.execute(
                    select(CopilotProposal).where(
                        CopilotProposal.tenant_id == ctx.tenant_id,
                        CopilotProposal.kind == "agent_builder",
                        CopilotProposal.session_id == seeded["session_id"],
                        CopilotProposal.created_at >= since,
                    )
                )
            )
            .scalars()
            .all()
        )
        drafts = [p for p in proposals if p.status == "draft"]
        first_ops = []
        for p in drafts:
            first = (
                await db.execute(
                    select(CopilotOperation)
                    .where(CopilotOperation.proposal_id == p.id)
                    .order_by(CopilotOperation.ordinal)
                    .limit(1)
                )
            ).scalar_one_or_none()
            first_ops.append(first.operation_type if first else None)
    rounds = question_rounds(calls)
    return [
        Check("exactly one open builder proposal", len(drafts) == 1, f"drafts={len(drafts)}"),
        Check(
            "the proposal creates an agent first",
            first_ops == ["agent.create"],
            f"first_ops={first_ops}",
        ),
        Check(f"at most {MAX_ROUNDS} question rounds", rounds <= MAX_ROUNDS, f"rounds={rounds}"),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    early = writes_or_connection_calls_before_proposal(
        list(ctx.run_context.get("toolCalls", [])), seeded.get("connection_scopes")
    )
    return [
        Check(
            "no write or connection call before the proposal",
            not early,
            f"writes_or_connection_calls={early}",
        )
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    return None
