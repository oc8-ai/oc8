"""B4 -- once-per-run missing-fact check before autonomous irreversible/outward calls."""

from __future__ import annotations

from oc8.agent.harness.stages.b_approval import posture
from oc8.modelrouter import NeutralMessage, ToolCall

_CLARIFICATION_BODY = """\
Proposed action:
{json}

List every fact this action depends on that is neither stated in the task nor
established by a tool result above (recipient, amount, date, which of several
candidates, wording constraints). Answer exactly NONE if there is none;
otherwise one line per missing fact."""

_FACT_CAP = 2000


def should_clarify(
    *,
    tier: str,
    autonomy: str,
    granted: bool,
    clarify_enabled: bool,
    already_done: bool,
) -> bool:
    if already_done or not clarify_enabled:
        return False
    if tier not in {"irreversible", "outward"}:
        return False
    return posture(tier, autonomy, granted=granted) == "allow"


def clarification_prompt(
    *,
    task_text: str,
    run_context: str,
    ledger_block: str,
    call_json: str,
) -> list[NeutralMessage]:
    return [
        NeutralMessage(role="user", content=f"Task:\n{task_text}"),
        NeutralMessage(role="user", content=run_context),
        NeutralMessage(role="user", content=ledger_block),
        NeutralMessage(
            role="user",
            content=_CLARIFICATION_BODY.format(json=call_json),
        ),
    ]


def parse_clarification(text: str) -> str | None:
    """None means proceed (NONE or empty). Otherwise the fact text, capped at 2000."""
    stripped = text.strip()
    if not stripped or stripped == "NONE":
        return None
    return stripped[:_FACT_CAP]


def substitute_call(tc: ToolCall, facts: str, *, chat: bool) -> ToolCall:
    if chat:
        return ToolCall(id=tc.id, name="ask_user", arguments={"question": facts})
    return ToolCall(
        id=tc.id,
        name="request_decision",
        arguments={
            "question": facts,
            "context": f"Missing facts before {tc.name}.",
            "options": [],
        },
    )
