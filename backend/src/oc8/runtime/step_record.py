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
already-solved concern and its CAPTURE is not built here:
`oc8.agent.harness.step_timing` already captures it (`start_step`/
`note_model`/`note_tools`/`finish_step`), wired into both runtimes' own step
loops and persisted through the run's existing context writes. Building a
second capture mechanism next to that one would be exactly the "two seams
that drift" failure this module's sibling concern (call state) exists to
avoid -- see `tests/agents/test_engine_step_timings.py` for the verification
that dev's existing capture already produces what this plan needs.

This module DOES own one small thing about that timing data, though:
`step_timing_dto` below, the snake_case-to-camelCase translation applied at
the wire boundary (both the `run_to_dto` fresh-`GET` path and every
`publish_run_step_timing` call site), for the same "one seam, not two"
reason as `call_state_for` -- see that function's own docstring.
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


def step_timing_dto(entry: dict[str, object]) -> dict[str, object]:
    """Translate one `stepTimings` entry from its stored, snake_case shape
    (written by `oc8.agent.harness.step_timing`'s `start_step`/`note_model`/
    `note_tools`/`finish_step` -- `step`, `model_wait_ms`, `ttft_ms`,
    `tool_wait_ms`, `step_wall_ms`) into the camelCase shape the wire (both
    `GET /runs/{id}`/`GET /chat/sessions/{sid}/runs/{rid}` via
    `api/v1/run.py`'s `run_to_dto`, and the live `run.step_timing` event
    published from `agent/engine.py` and `api/v1/internal_agent.py`) commits
    to.

    The stored snake_case keys must never change: the eval CLI
    (`backend/evals/oc8_evals/*`) and this module's own `latency_lines()`
    read them directly. This function is the one seam where the two shapes
    meet, so a fresh `GET` and a live-patched entry always agree on casing.
    """
    return {
        "step": entry.get("step"),
        "modelWaitMs": entry.get("model_wait_ms"),
        "ttftMs": entry.get("ttft_ms"),
        "toolWaitMs": entry.get("tool_wait_ms"),
        "stepWallMs": entry.get("step_wall_ms"),
    }
