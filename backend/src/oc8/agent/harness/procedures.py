"""Procedure satisfaction and checklist text (spec §7.2–7.3).

Pure helpers: satisfaction is recomputed from the ledger plus optional manual
marks. Denial, control tools, and runtime wiring live elsewhere.
"""

from __future__ import annotations

from oc8.agent.harness.state import Ledger, ProcedureMark
from oc8.skills.schema import Step


def satisfied_ids(
    steps: tuple[Step, ...], ledger: Ledger, mark: ProcedureMark | None
) -> frozenset[str]:
    evidence = mark.evidence if mark is not None else {}
    done: set[str] = set()
    for step in steps:
        if step.requires_kind == "read_of":
            if any(
                entity.kind == step.requires_value and entity.last_read_step is not None
                for entity in ledger.entities.values()
            ):
                done.add(step.id)
        elif step.requires_kind == "tool_called":
            if step.requires_value in ledger.tools_called:
                done.add(step.id)
        elif step.requires_kind == "confirmation":
            if any(decision.answered for decision in ledger.decisions):
                done.add(step.id)
        elif step.requires_kind == "manual":
            if step.id in evidence:
                done.add(step.id)
    return frozenset(done)


def missing_text(step: Step) -> str:
    if step.requires_kind == "read_of":
        return f"no {step.requires_value} record has been read in this run"
    if step.requires_kind == "tool_called":
        return f"tool {step.requires_value} has not been called in this run"
    if step.requires_kind == "confirmation":
        return "no confirmation has been answered in this run"
    return "this step has not been marked done"


def _gate_label(token: str) -> str:
    if token.startswith("tier:"):
        name = token.removeprefix("tier:")
        return f"{name} actions"
    return token


def checklist(steps: tuple[Step, ...], satisfied: frozenset[str]) -> str:
    lines = [
        "Procedure checklist (the system tracks these; a gated action is refused until",
        "the steps before it are done):",
    ]
    for index, step in enumerate(steps, start=1):
        mark = "[done]" if step.id in satisfied else "[ ]"
        line = f"{index}. {mark} {step.title}"
        if step.gates:
            labels = ", ".join(_gate_label(g) for g in step.gates)
            line = f"{line}  (gates: {labels})"
        lines.append(line)
    return "\n".join(lines)
