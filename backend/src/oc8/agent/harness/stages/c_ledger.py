"""Record successful tool effects on the run ledger."""

from __future__ import annotations

import hashlib

from oc8.agent.harness.state import DecisionRef, Ledger, OutwardRef


def render_ledger_block(ledger: Ledger) -> str:
    lines = [
        "# Working state (supersedes earlier working-state blocks)",
        "Records touched:",
    ]
    if ledger.entities:
        for key, entity in sorted(ledger.entities.items()):
            clauses: list[str] = []
            if entity.last_read_step is not None:
                clauses.append(f"read step {entity.last_read_step}")
            if entity.last_write_step is not None:
                tool = entity.write_tools[-1] if entity.write_tools else "a write"
                clauses.append(f"written step {entity.last_write_step} ({tool})")
            detail = f" — {', '.join(clauses)}" if clauses else ""
            if key in ledger.writes_unverified:
                detail += ", not re-read since"
            lines.append(
                f'- {entity.connection}/{entity.kind}/{entity.id} "{entity.label}"{detail}'
            )
    else:
        lines.append("- none")

    lines.append("Messages sent:")
    if ledger.outward:
        lines.extend(
            f"- {ref.connection} {ref.tool} to {ref.target} at step {ref.step}"
            for ref in ledger.outward
        )
    else:
        lines.append("- none")

    lines.append("Decisions requested:")
    if ledger.decisions:
        lines.extend(
            f"- {ref.tool} at step {ref.step}: {ref.question}" for ref in ledger.decisions
        )
    else:
        lines.append("- none")

    lines.append("Files produced:")
    if ledger.files:
        lines.extend(f"- {filename}" for filename in ledger.files)
    else:
        lines.append("- none")
    return "\n".join(lines)


def ledger_fingerprint(ledger: Ledger) -> str:
    return hashlib.sha256(render_ledger_block(ledger).encode()).hexdigest()


def record_outward(
    ledger: Ledger,
    *,
    connection: str,
    tool: str,
    target: str,
    step: int,
) -> None:
    ref = OutwardRef(
        connection=connection,
        tool=tool,
        target=target[:200],
        step=step,
    )
    if ref not in ledger.outward:
        ledger.outward.append(ref)


def record_file(ledger: Ledger, filename: str) -> None:
    if filename and filename not in ledger.files:
        ledger.files.append(filename)


def record_decision(
    ledger: Ledger,
    *,
    tool: str,
    question: str,
    step: int,
) -> None:
    ref = DecisionRef(tool=tool, question=question[:200], step=step)
    if ref not in ledger.decisions:
        ledger.decisions.append(ref)
