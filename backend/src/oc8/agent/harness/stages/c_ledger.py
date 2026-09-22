"""Record successful tool effects on the run ledger."""

from __future__ import annotations

from oc8.agent.harness.state import DecisionRef, Ledger, OutwardRef


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
