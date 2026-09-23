"""Procedure satisfaction, B3 denial, checklist, and C5 flip lines (spec §7).

Pure helpers: satisfaction is recomputed from the ledger plus optional manual
marks. Runtime wiring lives elsewhere.
"""

from __future__ import annotations

from collections.abc import Sequence

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


def procedure_denial(
    *,
    tool: str,
    tier: str,
    skills: Sequence[tuple[str, str, tuple[Step, ...]]],
    ledger: Ledger,
    procedure: dict[str, ProcedureMark],
) -> tuple[str, str] | None:
    """Return (reason, skill_slug) or None. Reason is ruling 7's full sentence."""
    tier_token = f"tier:{tier}"
    for slug, display_name, steps in skills:
        if not steps:
            continue
        done = satisfied_ids(steps, ledger, procedure.get(slug))
        for index, step in enumerate(steps):
            if tool not in step.gates and tier_token not in step.gates:
                continue
            for earlier_k, earlier in enumerate(steps[:index], start=1):
                if not earlier.required or earlier.id in done:
                    continue
                missing = missing_text(earlier)
                reason = (
                    f'Procedure "{display_name}", step {earlier_k} "{earlier.title}" '
                    f"is not yet satisfied: {missing}. "
                    "Complete it, then retry. This is a policy decision, not a tool error."
                )
                return reason, slug
    return None


def newly_satisfied_lines(
    *,
    skills: Sequence[tuple[str, str, tuple[Step, ...]]],
    before: dict[str, frozenset[str]],
    after: dict[str, frozenset[str]],
) -> list[str]:
    lines: list[str] = []
    for slug, display_name, steps in skills:
        prev = before.get(slug, frozenset())
        now = after.get(slug, frozenset())
        for index, step in enumerate(steps):
            if step.id not in now or step.id in prev:
                continue
            k = index + 1
            if index + 1 < len(steps):
                nxt = steps[index + 1]
                next_part = f"{k + 1} '{nxt.title}'"
            else:
                next_part = "(none)"
            lines.append(
                f"Procedure '{display_name}': step {k} '{step.title}' satisfied; "
                f"next: {next_part}"
            )
    return lines


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
