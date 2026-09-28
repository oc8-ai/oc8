"""Per-tool-call derived state, shared by BOTH runtimes.

`agent_run.context["toolCalls"]` already carries `tool`/`arguments`/`result`
from each runtime's own shared append site
(`oc8.runtime.run_context.append_tool_call`, called by both `agent/engine.py`
and `api/v1/internal_agent.py`). Later work on the run step timeline adds
`step`, `connection` and `state` to every entry so the timeline can join a
call to a step's timing and show whether it ran, failed, was denied, or is
waiting on a human. This module owns the one derived field the two runtimes
must agree on -- `state` -- plus the key set every entry is expected to carry.

Step-level TIMING (`stepTimings`, one entry per model step) is a SEPARATE,
already-solved concern and is not built here: `oc8.agent.harness.step_timing`
already captures it (`start_step`/`note_model`/`note_tools`/`finish_step`),
wired into both runtimes' own step loops and persisted through the run's
existing context writes. Building a second capture mechanism next to that one
would be exactly the "two seams that drift" failure this module's sibling
concern (call state) exists to avoid -- see
`tests/agents/test_engine_step_timings.py` for the verification that dev's
existing capture already produces what this plan needs.
"""

from __future__ import annotations

#: The per-call states a `toolCalls` entry may carry. `done`/`failed` are
#: outcomes of a call that ran; `denied` is a call refused before dispatch
#: (a guardrail DENY, a blocking plugin hook, no tool server, an
#: already-delivered outward refusal); `awaiting_approval` is a call parked
#: for a human and not yet run at all.
CALL_STATES: frozenset[str] = frozenset({"done", "failed", "denied", "awaiting_approval"})

#: Keys EVERY `toolCalls` entry carries, in both runtimes. The parity test
#: asserts this set against entries produced by each runtime; timing keys
#: (`startedAt`/`durationMs`) are deliberately NOT here -- they are present
#: only for a call that was actually dispatched.
REQUIRED_CALL_KEYS: frozenset[str] = frozenset({"tool", "arguments", "step", "connection", "state"})


def call_state_for(output: str, *, dispatched: bool) -> str:
    """The state of a call that reached a runtime's shared append site.

    `dispatched=False` means one of the pre-dispatch refusal branches ran, so
    nothing was attempted: that is a denial, not a failure. A dispatched call
    whose output came back as an `ERROR:` string failed. Both runtimes call
    this rather than each writing the same two-line conditional, because the
    two of them disagreeing about what "failed" means is a bug nobody would
    see until a customer asked why one runtime's timeline is all red.
    """
    if not dispatched:
        return "denied"
    return "failed" if output.startswith("ERROR:") else "done"
