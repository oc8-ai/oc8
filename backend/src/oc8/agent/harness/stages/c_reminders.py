"""C5 -- advisory reminders injected after a tool result (spec §6 C5).

Every function here is shared verbatim by the in-process engine and the
isolated runtime's /tool endpoint; they were moved out of engine.py without a
change of behaviour. The per-result cap, the one-time cumulative budget note
and the consecutive-identical-call nudge never block or rewrite a call, only
what the model reads next.
"""

from __future__ import annotations

import json
from typing import Any

from oc8.agent.harness.calls import call_sig
from oc8.modelrouter import ToolCall

#: Consecutive-identical-call counts that trigger a repeat-call reminder (see
#: track_repeat_tool_call). The first is a short nudge; the later two spell out
#: the tool, count and arguments -- by then a short nudge already failed once.
REPEAT_CALL_THRESHOLDS = (3, 5, 8)
_REPEAT_ARGS_PREVIEW_CHARS = 500


def track_repeat_tool_call(
    state: dict[str, Any], tc: ToolCall
) -> tuple[dict[str, Any], str | None]:
    """Advisory loop-hygiene guard: counts CONSECUTIVE calls to the same tool
    with canonically-identical arguments (via _call_sig, so this agrees with the
    approval-resume matcher on what "identical" means) and, once the count
    crosses a threshold, returns a reminder to inject -- never blocks or
    rewrites the call itself, only nudges the model to look at what it already
    has instead of repeating itself.

    Shared VERBATIM by the in-process engine (loop()'s own `_repeat_state`, a
    plain local dict) and the isolated runtime's /tool endpoint (persisted on
    run.context so it survives across that runtime's separate HTTP requests) --
    see "container parity is not automatic": duplicating this logic instead of
    sharing it is exactly how the two runtimes drift.

    `state` is `{"sig": str | None, "count": int}` (JSON-serializable on
    purpose, for the isolated runtime's context column) or `{}` for a fresh
    run. Returns the updated state and the reminder text, or None if no
    threshold was crossed this call.
    """
    sig = call_sig(tc)
    prior_count = state.get("count", 0) if state.get("sig") == sig else 0
    count = prior_count + 1
    new_state = {"sig": sig, "count": count}
    if count not in REPEAT_CALL_THRESHOLDS:
        return new_state, None
    if count == REPEAT_CALL_THRESHOLDS[0]:
        return new_state, (
            "You are repeating the exact same tool call with identical "
            "arguments. Carefully analyze the previous result before calling "
            "again -- if it already answered your question, act on it instead "
            "of repeating the call."
        )
    args_preview = json.dumps(tc.arguments, sort_keys=True, default=str)
    if len(args_preview) > _REPEAT_ARGS_PREVIEW_CHARS:
        args_preview = args_preview[: _REPEAT_ARGS_PREVIEW_CHARS - 1] + "…"
    return new_state, (
        f"You have now called '{tc.name}' {count} times in a row with the "
        f"exact same arguments ({args_preview}). This strongly suggests you "
        "are stuck in a loop. Stop and reconsider: either the result you "
        "already have answers this, or the call cannot succeed and you should "
        "try a different approach or explain the blocker instead of repeating it."
    )


#: Tool results are appended to the transcript verbatim and replayed on every
#: subsequent turn -- an unaggregated report page (e.g. a groupby result with
#: hundreds of nested rows) can alone run into tens of thousands of
#: characters, and a few such pages compound fast. Capped, not dropped: the
#: model still gets most of one big result plus an explicit note that it was
#: cut, so it learns to narrow the query instead of silently losing data with
#: no visible cause -- see the 2026-09-15 oc8-obs incident, where an uncapped
#: 1400-row pagination loop left no room for the model's own answer and the
#: run failed with no error recorded anywhere (the truncated_empty path below
#: this module's step loop, and internal_agent.py's identical one, produced
#: an empty output rather than a diagnosable message).
MAX_TOOL_RESULT_CHARS = 20_000


def cap_tool_output(output: str) -> str:
    """Bound a single tool result before it enters the transcript. Shared
    verbatim by the in-process engine and the isolated runtime's /tool
    endpoint, same reasoning as todo_continuation_reminder above."""
    if len(output) <= MAX_TOOL_RESULT_CHARS:
        return output
    omitted = len(output) - MAX_TOOL_RESULT_CHARS
    return (
        f"{output[:MAX_TOOL_RESULT_CHARS]}\n\n"
        f"[... {omitted} more characters omitted -- this result was too large to include "
        "in full. Narrow the query (a smaller date range, fewer groupby dimensions, or a "
        "lower limit) instead of paging through it in full.]"
    )


#: Warned once per run when tool results have cumulatively used a large slice
#: of a typical context window, well before the model actually runs out of
#: room -- the same incident MAX_TOOL_RESULT_CHARS documents showed that
#: hitting the wall produces no error at all, just a silently empty answer,
#: so the model needs the nudge while it can still act on it.
TOOL_OUTPUT_BUDGET_WARNING_CHARS = 150_000


def tool_output_budget_reminder(total_chars: int) -> str:
    """Reminder injected the first time this run's cumulative tool-result size
    crosses TOOL_OUTPUT_BUDGET_WARNING_CHARS. Shared verbatim by the in-process
    engine and the isolated runtime's /tool endpoint, same reasoning as
    todo_continuation_reminder above."""
    return (
        f"[System note: tool results in this run have grown to roughly {total_chars:,} "
        "characters so far. If you are paging through a report or list, stop and switch "
        "to a narrower query or a server-side aggregation instead of continuing to page "
        "-- an oversized transcript can silently exhaust your own response budget later "
        "in this run, with no error message.]"
    )
