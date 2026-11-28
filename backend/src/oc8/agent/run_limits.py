"""Where a run stops besides its step budget, for every runtime alike.

Two stops, both checked once per step before the next model call:

* the tokens the run has used so far, against `definition["max_run_tokens"]`
  (per agent) or `agent_max_run_tokens` (the framework default; 0 is no
  limit). Every step sends the whole conversation again -- the instructions,
  the skills, the tool list and every earlier result -- so a run's cost grows
  with its steps far faster than the answer limit (`max_tokens`) suggests;
* the same tool called with the same arguments `REPEAT_LIMIT` times in a row.
  The repeat reminders (`harness.stages.c_reminders`) already nudge the model
  at 3, 5 and 8 identical calls; a run that is still repeating two calls after
  the last nudge is going round in circles, and only stopping it helps.

The engine (`agent/engine.py`) and the isolated runtime
(`api/v1/internal_agent.py`) both ask `stop_reason`, so the two never stop a
run for different reasons. The outputs are what the run reports and what
`notifications.events` sorts into the step-limit category.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from oc8.agent.harness.calls import call_sig
from oc8.agent.harness.stages.c_reminders import REPEAT_CALL_THRESHOLDS
from oc8.config import get_settings
from oc8.modelrouter import ToolCall

TOKEN_LIMIT_OUTPUT = "Reached token limit."
NO_PROGRESS_OUTPUT = "Stopped: the same call kept repeating without progress."

#: Identical calls in a row that count as going round in circles: two more
#: than the last repeat reminder, so every reminder had its chance first.
REPEAT_LIMIT = REPEAT_CALL_THRESHOLDS[-1] + 2


def max_run_tokens(definition: Mapping[str, Any] | None) -> int | None:
    """The run's token limit: the agent's own, else the framework default;
    None when neither sets one."""
    override = (definition or {}).get("max_run_tokens")
    if isinstance(override, int) and override > 0:
        return override
    default = get_settings().agent_max_run_tokens
    return default if default > 0 else None


def run_tokens(step_timings: Iterable[Mapping[str, Any]]) -> int:
    """Input plus output tokens of every step that recorded its usage."""
    return sum(
        int(step.get("tokens_in") or 0) + int(step.get("tokens_out") or 0) for step in step_timings
    )


def _fingerprint(call: Mapping[str, Any]) -> str:
    """ "Identical" as the repeat reminders and approval matching mean it."""
    arguments = call.get("arguments")
    return call_sig(
        ToolCall(
            id="",
            name=str(call.get("tool") or ""),
            arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
        )
    )


def repeated_call(tool_calls: Sequence[Mapping[str, Any]]) -> str | None:
    """The tool's name when the last `REPEAT_LIMIT` calls are the same call."""
    if len(tool_calls) < REPEAT_LIMIT:
        return None
    last = tool_calls[-REPEAT_LIMIT:]
    first = _fingerprint(last[0])
    if all(_fingerprint(call) == first for call in last[1:]):
        return str(last[0].get("tool") or "")
    return None


def stop_reason(
    definition: Mapping[str, Any] | None,
    step_timings: Iterable[Mapping[str, Any]],
    tool_calls: Sequence[Mapping[str, Any]],
) -> str | None:
    """The output to stop the run with before its next step, or None."""
    limit = max_run_tokens(definition)
    if limit is not None and run_tokens(step_timings) >= limit:
        return TOKEN_LIMIT_OUTPUT
    if repeated_call(tool_calls) is not None:
        return NO_PROGRESS_OUTPUT
    return None
