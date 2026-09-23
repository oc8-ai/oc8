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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from oc8.agent.harness.caps import ModelCaps
from oc8.agent.harness.procedures import (
    PROCEDURE_CONTINUATION_MAX_ROUNDS,
    open_required_procedures,
    procedure_continuation_reminder,
    procedure_denial,
    procedure_exhausted_note,
)
from oc8.agent.harness.prompts import format_step_stamp, resolve_timezone
from oc8.agent.harness.stages.b_approval import posture, render_preview
from oc8.agent.harness.stages.b_read_before_write import read_before_write_denial
from oc8.agent.harness.stages.c_errors import ToolError, render_error
from oc8.agent.harness.stages.c_provenance import fence_external
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
from oc8.agent.harness.stages.d_verify import (
    VERIFY_MAX_ROUNDS,
    verify_exhausted_note,
    verify_reminder,
)
from oc8.agent.harness.state import HarnessState, Ledger
from oc8.modelrouter import ToolCall
from oc8.skills.schema import Step


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


@dataclass(frozen=True)
class GateVerdict:
    effect: Literal["allow", "ask", "deny"]
    reason: str = ""
    rule: str = ""
    preview: str = ""
    justification: str = ""
    tier: str = "write"


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

    # ----------------------------------------------------------------- phase B

    def gate(
        self,
        tc: ToolCall,
        *,
        tier: str,
        ledger: Ledger,
        connection: str,
        config: dict[str, Any] | None,
        autonomy: str,
        granted: bool,
        record_label: str,
        identity: tuple[str, str] | None,
        procedures: Sequence[tuple[str, str, tuple[Step, ...]]] | None = None,
    ) -> GateVerdict:
        if procedures is not None:
            hit = procedure_denial(
                tool=tc.name,
                tier=tier,
                skills=procedures,
                ledger=ledger,
                procedure=self.state.procedure,
            )
            if hit is not None:
                reason, _slug = hit
                return GateVerdict(
                    effect="deny",
                    reason=reason,
                    rule="procedure",
                    tier=tier,
                )

        denial = read_before_write_denial(
            tool=tc.name,
            tier=tier,
            identity=identity,
            ledger=ledger,
            connection=connection,
            config=config,
            label=record_label,
        )
        if denial is not None:
            suffix = (
                "This is a policy decision, not a tool error — "
                "do not retry the same change another way."
            )
            if suffix not in denial:
                denial = f"{denial.rstrip()} {suffix}"
            return GateVerdict(
                effect="deny",
                reason=denial,
                rule="read_before_write",
                tier=tier,
            )

        if posture(tier, autonomy, granted=granted) == "ask":
            cfg = config or {}
            templates = cfg.get("approval_templates")
            template = templates.get(tc.name) if isinstance(templates, dict) else None
            return GateVerdict(
                effect="ask",
                preview=render_preview(
                    tool=tc.name,
                    connection=connection,
                    arguments=tc.arguments,
                    template=str(template) if template is not None else None,
                    record=record_label,
                ),
                tier=tier,
            )

        return GateVerdict(effect="allow", tier=tier)

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
        """C2 + C6 + C1 + C3 + C5: shape failures, fence successful external
        results, spill large results, stamp, count against the per-run budget,
        and track consecutive identical calls. Reminder order remains repeat
        nudge, then budget note.
        """
        if error is not None:
            output = render_error(tc.name, source, error)
        elif source != "oc8":
            output = fence_external(output, source=f"{source}:{tc.name}")
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
        self,
        open_todos: list[dict[str, str]],
        *,
        can_continue: bool = True,
        procedures: Sequence[tuple[str, str, tuple[Step, ...]]] | None = None,
    ) -> FinishVerdict:
        """Run D1 todo continuation, then D2 procedure, then D3 verification.

        `can_continue` is the caller's own step budget (the isolated runtime
        checks it explicitly; the in-process loop bound handles it).
        """
        if open_todos and can_continue and self.state.todo_rounds < TODO_CONTINUATION_MAX_ROUNDS:
            self.state.todo_rounds += 1
            return FinishVerdict(
                ok=False,
                reminder=todo_continuation_reminder(open_todos, self.state.todo_rounds),
            )
        d1_note = todo_continuation_exhausted_note(open_todos) if open_todos else None

        open_proc: list[tuple[str, str, str]] = []
        if procedures:
            open_proc = open_required_procedures(
                procedures,
                ledger=self.state.ledger,
                procedure=self.state.procedure,
            )
        if (
            open_proc
            and can_continue
            and self.state.procedure_rounds < PROCEDURE_CONTINUATION_MAX_ROUNDS
        ):
            self.state.procedure_rounds += 1
            return FinishVerdict(
                ok=False,
                reminder=procedure_continuation_reminder(
                    open_proc, self.state.procedure_rounds
                ),
            )
        d2_note = procedure_exhausted_note(open_proc) if open_proc else None

        pending = [
            self.state.ledger.entities[key]
            for key in self.state.ledger.writes_unverified
            if key in self.state.ledger.entities
        ]
        self.state.ledger.writes_unverified = [
            key
            for key in self.state.ledger.writes_unverified
            if key in self.state.ledger.entities
        ]
        d3_note = None
        if pending and can_continue and self.state.verify_rounds < VERIFY_MAX_ROUNDS:
            self.state.verify_rounds += 1
            return FinishVerdict(ok=False, reminder=verify_reminder(pending))
        if pending:
            d3_note = verify_exhausted_note(pending)
        notes = [note for note in (d1_note, d2_note, d3_note) if note]
        return FinishVerdict(ok=True, exhausted_note="\n\n".join(notes) or None)
