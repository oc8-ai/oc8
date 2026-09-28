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
class EntityRef:
    connection: str
    kind: str
    id: str
    label: str = ""
    first_read_step: int | None = None
    last_read_step: int | None = None
    last_write_step: int | None = None
    write_tools: list[str] = field(default_factory=list)


@dataclass
class OutwardRef:
    connection: str
    tool: str
    target: str
    step: int


@dataclass
class DecisionRef:
    tool: str
    question: str
    step: int
    #: True once the human has answered; False when only the ask was recorded.
    answered: bool = False


@dataclass
class Ledger:
    entities: dict[str, EntityRef] = field(default_factory=dict)
    writes_unverified: list[str] = field(default_factory=list)
    outward: list[OutwardRef] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    decisions: list[DecisionRef] = field(default_factory=list)
    #: Tool names that returned a successful result this run (once each).
    tools_called: list[str] = field(default_factory=list)


@dataclass
class ProcedureMark:
    #: Manual step_id -> evidence text (capped at 500 chars by the writer).
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass
class MaskRef:
    step: int
    tool: str
    chars: int
    filename: str | None = None


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
    #: Re-verification nudges issued so far (bounded independently of steps).
    verify_rounds: int = 0
    #: Procedure-continuation nudges issued so far (bounded independently of steps).
    procedure_rounds: int = 0
    #: The current step number, set by the caller at the top of each turn, for
    #: C3's step stamp. 0 on a fresh run, before the first turn sets it.
    step_no: int = 0
    ledger: Ledger = field(default_factory=Ledger)
    ledger_sent_hash: str = ""
    masked: dict[str, MaskRef] = field(default_factory=dict)
    #: Manual procedure evidence keyed by skill slug (satisfaction is recomputed).
    procedure: dict[str, ProcedureMark] = field(default_factory=dict)
    compactions: int = 0
    last_prompt_tokens: int = 0
    last_compacted_step: int = -999
    #: Tool names pinned by find_tools for later steps' inline core set.
    pinned_tools: list[str] = field(default_factory=list)
    #: Deferred tool catalog for find_tools (empty when not deferring).
    tool_catalog: list[dict[str, Any]] = field(default_factory=list)
    #: Connections whose tools/list failed. find_tools repeats the sentence.
    unavailable_connections: list[dict[str, Any]] = field(default_factory=list)
    #: True after the once-per-run B4 missing-fact check has fired.
    clarification_done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> HarnessState:
        if not raw:
            return cls()
        ledger_raw = raw.get("ledger") or {}
        entities_raw = ledger_raw.get("entities") or {}
        ledger = Ledger(
            entities={
                key: EntityRef(
                    connection=str(value["connection"]),
                    kind=str(value["kind"]),
                    id=str(value["id"]),
                    label=str(value.get("label", "")),
                    first_read_step=value.get("first_read_step"),
                    last_read_step=value.get("last_read_step"),
                    last_write_step=value.get("last_write_step"),
                    write_tools=list(value.get("write_tools") or []),
                )
                for key, value in entities_raw.items()
            },
            writes_unverified=list(ledger_raw.get("writes_unverified") or []),
            outward=[
                OutwardRef(
                    connection=str(value["connection"]),
                    tool=str(value["tool"]),
                    target=str(value["target"]),
                    step=int(value["step"]),
                )
                for value in ledger_raw.get("outward") or []
            ],
            files=list(ledger_raw.get("files") or []),
            decisions=[
                DecisionRef(
                    tool=str(value["tool"]),
                    question=str(value["question"]),
                    step=int(value["step"]),
                    answered=bool(value.get("answered", False)),
                )
                for value in ledger_raw.get("decisions") or []
            ],
            tools_called=list(ledger_raw.get("tools_called") or []),
        )
        procedure_raw = raw.get("procedure") or {}
        procedure = {
            str(slug): ProcedureMark(
                evidence={
                    str(step_id): str(text)
                    for step_id, text in (mark.get("evidence") or {}).items()
                }
            )
            for slug, mark in procedure_raw.items()
            if isinstance(mark, dict)
        }
        return cls(
            version=int(raw.get("version", HARNESS_STATE_VERSION)),
            tool_output_chars=int(raw.get("tool_output_chars", 0)),
            tool_output_budget_warned=bool(raw.get("tool_output_budget_warned", False)),
            repeat=dict(raw.get("repeat") or {}),
            todo_rounds=int(raw.get("todo_rounds", 0)),
            verify_rounds=int(raw.get("verify_rounds", 0)),
            procedure_rounds=int(raw.get("procedure_rounds", 0)),
            step_no=int(raw.get("step_no", 0)),
            ledger=ledger,
            ledger_sent_hash=str(raw.get("ledger_sent_hash", "")),
            procedure=procedure,
            compactions=int(raw.get("compactions", 0)),
            last_prompt_tokens=int(raw.get("last_prompt_tokens", 0)),
            last_compacted_step=int(raw.get("last_compacted_step", -999)),
            pinned_tools=list(raw.get("pinned_tools") or []),
            tool_catalog=[dict(card) for card in (raw.get("tool_catalog") or [])],
            unavailable_connections=[
                dict(item) for item in (raw.get("unavailable_connections") or []) if isinstance(item, dict)
            ],
            clarification_done=bool(raw.get("clarification_done", False)),
            masked={
                key: MaskRef(
                    step=int(value["step"]),
                    tool=str(value["tool"]),
                    chars=int(value["chars"]),
                    filename=(
                        str(value["filename"]) if value.get("filename") is not None else None
                    ),
                )
                for key, value in (raw.get("masked") or {}).items()
            },
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
