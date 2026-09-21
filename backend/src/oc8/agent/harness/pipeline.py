"""The harness pipeline (spec §3.2).

Package 1 carries the two stateful phases both runtimes share today: C
(`shape`, the C5 reminders) and D (`may_finish`, the D1 todo continuation).
The B-stages live as plain functions in `stages/` for now because the two
runtimes call them at two different points of their loops (authorisation
before the hooks and the approval branch, the record guards right before
dispatch); folding them into one `gate()` would reorder hook execution, which
package 1 must not do. Package 5 introduces `gate()` when B1-B5 land.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oc8.agent.harness.caps import ModelCaps
from oc8.agent.harness.prompts import format_step_stamp, resolve_timezone
from oc8.agent.harness.stages.c_errors import ToolError, render_error
from oc8.agent.harness.stages.c_reminders import (
    TOOL_OUTPUT_BUDGET_WARNING_CHARS,
    tool_output_budget_reminder,
    track_repeat_tool_call,
)
from oc8.agent.harness.stages.c_spill import Spill, maybe_spill
from oc8.agent.harness.stages.d_todo import (
    TODO_CONTINUATION_MAX_ROUNDS,
    todo_continuation_exhausted_note,
    todo_continuation_reminder,
)
from oc8.agent.harness.state import HarnessState
from oc8.modelrouter import ToolCall


@dataclass
class ShapedResult:
    #: What goes into the tool message.
    output: str
    #: User-role messages to append after the tool message, in this order.
    reminders: list[str] = field(default_factory=list)
    #: Full tool result to persist when the output is represented by a preview.
    spill: Spill | None = None


@dataclass(frozen=True)
class FinishVerdict:
    ok: bool
    #: When not ok: the continuation nudge to append as a user message.
    reminder: str | None = None
    #: When ok but todos are still open: appended to the run's output so a run
    #: that gave up does not look like one that finished.
    exhausted_note: str | None = None


class Harness:
    def __init__(self, *, state: HarnessState | None = None, caps: ModelCaps | None = None) -> None:
        self.state = state if state is not None else HarnessState()
        self.caps = caps if caps is not None else ModelCaps()

    @classmethod
    def from_run_context(cls, ctx: dict[str, Any], *, caps: ModelCaps | None = None) -> Harness:
        """The isolated runtime's constructor: state travels on `run.context`
        between its /step and /tool requests."""
        return cls(state=HarnessState.from_run_context(ctx), caps=caps)

    def store(self, ctx: dict[str, Any]) -> None:
        self.state.store(ctx)

    # ----------------------------------------------------------------- phase C

    def shape(
        self,
        tc: ToolCall,
        output: str,
        *,
        max_steps: int,
        tz: str = "UTC",
        source: str = "oc8",
        error: ToolError | None = None,
    ) -> ShapedResult:
        """C2 + C1 + C3 + C5: shape failures, spill large results, stamp, count it
        against the per-run budget, track the consecutive-identical-call
        state. Order of the reminders matches what both runtimes appended
        before the move: repeat nudge, then budget note.
        """
        if error is not None:
            output = render_error(tc.name, source, error)
        tz_label, resolved_tz = resolve_timezone(tz)
        now = dt.datetime.now(resolved_tz)
        stamp = format_step_stamp(
            step_no=self.state.step_no, max_steps=max_steps, now=now, tz_label=tz_label
        )
        preview, spill = maybe_spill(tc.name, output, step_no=self.state.step_no)
        stamped = f"{stamp} {preview}"
        self.state.tool_output_chars += len(stamped)
        budget_note: str | None = None
        if (
            not self.state.tool_output_budget_warned
            and self.state.tool_output_chars >= TOOL_OUTPUT_BUDGET_WARNING_CHARS
        ):
            self.state.tool_output_budget_warned = True
            budget_note = tool_output_budget_reminder(self.state.tool_output_chars)
        self.state.repeat, repeat_reminder = track_repeat_tool_call(self.state.repeat, tc)
        reminders = [r for r in (repeat_reminder, budget_note) if r is not None]
        return ShapedResult(output=stamped, reminders=reminders, spill=spill)

    # ----------------------------------------------------------------- phase D

    def may_finish(
        self, open_todos: list[dict[str, str]], *, can_continue: bool = True
    ) -> FinishVerdict:
        """D1: refuse to let the model finish while its own todo_write list has
        open items, up to TODO_CONTINUATION_MAX_ROUNDS times. `can_continue` is
        the caller's own step budget (the isolated runtime checks it explicitly;
        the in-process loop bound handles it)."""
        if open_todos and can_continue and self.state.todo_rounds < TODO_CONTINUATION_MAX_ROUNDS:
            self.state.todo_rounds += 1
            return FinishVerdict(
                ok=False,
                reminder=todo_continuation_reminder(open_todos, self.state.todo_rounds),
            )
        if open_todos:
            return FinishVerdict(
                ok=True, exhausted_note=todo_continuation_exhausted_note(open_todos)
            )
        return FinishVerdict(ok=True)
