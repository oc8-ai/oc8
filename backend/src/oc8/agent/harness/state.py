"""Per-run harness state (spec §3.3).

Held in memory by the in-process engine for the lifetime of one `run_agent`
call, and persisted under `run.context["harness"]` by the isolated runtime,
whose /step and /tool endpoints are separate HTTP requests with no loop to
hold anything between them. JSON-serialisable on purpose -- every field must
survive a JSONB round trip unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

HARNESS_STATE_VERSION = 1
#: Key under `run.context` the isolated runtime keeps this state in.
CONTEXT_KEY = "harness"
#: Top-level `run.context` keys the isolated runtime used before the state
#: moved under CONTEXT_KEY. Read once on load and removed on store, so a run
#: already in flight at deploy time keeps its counters.
_LEGACY_KEYS = ("tool_output_chars", "tool_output_budget_warned", "repeat_tracker")


@dataclass
class HarnessState:
    version: int = HARNESS_STATE_VERSION
    #: Cumulative size of tool results entered into the transcript this run
    #: (after the per-result cap), for the one-time budget reminder.
    tool_output_chars: int = 0
    tool_output_budget_warned: bool = False
    #: `{"sig": str, "count": int}` for the consecutive-identical-call tracker,
    #: `{}` on a fresh run.
    repeat: dict[str, Any] = field(default_factory=dict)
    #: Todo-continuation nudges issued so far (bounded independently of steps).
    todo_rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> HarnessState:
        if not raw:
            return cls()
        return cls(
            version=int(raw.get("version", HARNESS_STATE_VERSION)),
            tool_output_chars=int(raw.get("tool_output_chars", 0)),
            tool_output_budget_warned=bool(raw.get("tool_output_budget_warned", False)),
            repeat=dict(raw.get("repeat") or {}),
            todo_rounds=int(raw.get("todo_rounds", 0)),
        )

    @classmethod
    def from_run_context(cls, ctx: dict[str, Any]) -> HarnessState:
        """Load from `ctx[CONTEXT_KEY]`; fall back to the legacy top-level keys
        for a run that started before the state moved."""
        if CONTEXT_KEY in ctx:
            return cls.from_dict(ctx[CONTEXT_KEY])
        return cls(
            tool_output_chars=int(ctx.get("tool_output_chars", 0)),
            tool_output_budget_warned=bool(ctx.get("tool_output_budget_warned", False)),
            repeat=dict(ctx.get("repeat_tracker") or {}),
        )

    def store(self, ctx: dict[str, Any]) -> None:
        """Write back into `ctx` (mutates), retiring the legacy keys."""
        ctx[CONTEXT_KEY] = self.to_dict()
        for key in _LEGACY_KEYS:
            ctx.pop(key, None)
