"""D1 -- todo continuation (spec §6 D1).

Moved out of engine.py unchanged; shared verbatim by the in-process engine and
the isolated runtime's /step endpoint.
"""

from __future__ import annotations

#: Ported from DeepSeek Harness's goal-round-driver, adapted to oc8's bounded
#: step loop: there is no separate session-level "goal" object here, no idle
#: detection, and no multi-session resume -- a run is already one bounded
#: execution with its own step budget. Reusing the already-model-facing
#: `todo_write` list as the completion signal (instead of porting a whole
#: goal domain/service/UI) is the Keep-It-Simple call: an agent that never
#: calls todo_write gets zero behavior change, and one that does gets the
#: harness refusing to let it stop while its own declared checklist still has
#: open items -- directly the Kai bug pattern (a status report written with
#: tickets still pending). Bounded independently of max_steps so a stubborn
#: model cannot burn a whole run's budget on reminders alone; each round still
#: also counts as one ordinary step against max_steps.
TODO_CONTINUATION_MAX_ROUNDS = 3


def todo_continuation_reminder(open_todos: list[dict[str, str]], round_no: int) -> str:
    """Reminder injected when the model tries to finish a run while its own
    todo_write list still has open (non-completed) items -- see
    TODO_CONTINUATION_MAX_ROUNDS. Shared verbatim by the in-process engine and
    the isolated runtime's /step endpoint, same reasoning as
    track_repeat_tool_call above."""
    lines = "\n".join(
        f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in open_todos
    )
    return (
        f"You indicated you are finished, but {len(open_todos)} todo item(s) from your own "
        f"todo_write list are still open (continuation round {round_no}/"
        f"{TODO_CONTINUATION_MAX_ROUNDS}):\n{lines}\n"
        "Continue working through them. If any are genuinely done, no longer applicable, "
        "or blocked, call todo_write again to update their status and explain why before "
        "finishing."
    )


def todo_continuation_exhausted_note(open_todos: list[dict[str, str]]) -> str:
    """Appended to the run's own output when it ends with todo_write items still
    open despite TODO_CONTINUATION_MAX_ROUNDS worth of nudging -- without this, a
    run that gave up looks identical to one that genuinely finished everything.
    Shared verbatim by the in-process engine and the isolated runtime's /step
    endpoint, same reasoning as todo_continuation_reminder above."""
    lines = "\n".join(
        f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in open_todos
    )
    return (
        f"[Note: this run ended with {len(open_todos)} todo item(s) still open after "
        f"{TODO_CONTINUATION_MAX_ROUNDS} continuation attempt(s):\n{lines}]"
    )
