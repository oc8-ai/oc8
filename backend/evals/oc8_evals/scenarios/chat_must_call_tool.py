"""chat_harness / must_call_tool: a figure about budgets, usage, runs or
records must come from a tool result of this turn (spec §5.9 grounding rule).

The member asks the Copilot for a figure. Scored on the run's own tool calls,
not on what the answer says: one of `params.tools` was called. `params.stale`
seeds an earlier exchange in which the Copilot already quoted a figure, so
reusing it instead of looking again is the failure being measured;
`params.forbid` then names that stale figure, which the new answer must not
repeat.
"""

from __future__ import annotations

from typing import Any

from oc8_evals.scenarios import _chat
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    if not _chat.chat(ctx).assistant:
        raise RuntimeError("must_call_tool runs on the Copilot; set params.agent = 'copilot'")
    stale = ctx.task.params.get("stale")
    if stale:
        await _chat.seed_turns(ctx, [(str(role), str(text)) for role, text in stale])
    return {}


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    wanted = {str(t) for t in ctx.task.params.get("tools", [])}
    called = _chat.called_tools(ctx)
    text = await _chat.reply_text(ctx)
    return [
        _chat.answered(ctx, text),
        Check(
            f"called one of {sorted(wanted)}",
            bool(wanted & set(called)),
            f"called={called}",
        ),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    text = await _chat.reply_text(ctx)
    _, bad = _chat.reply_checks(text, expect=[], forbid=list(ctx.task.params.get("forbid", [])))
    return bad


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    return None
