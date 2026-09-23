"""Procedure satisfaction and checklist rendering (Package 9 Task 2)."""

from __future__ import annotations

from typing import Any

from oc8.agent.harness.procedures import checklist, missing_text, satisfied_ids
from oc8.agent.harness.state import DecisionRef, EntityRef, Ledger, ProcedureMark
from oc8.skills.schema import Step, parse_definition

SPEC_STEPS_RAW: list[dict[str, Any]] = [
    {
        "id": "identify",
        "title": "Identify the customer record",
        "requires": {"read_of": "partner"},
        "required": True,
    },
    {
        "id": "confirm_scope",
        "title": "Confirm the discount with the requester",
        "requires": {"confirmation": True},
    },
    {
        "id": "quote",
        "title": "Create the quotation",
        "requires": {"tool_called": "create_quotation"},
        "gates": ["create_quotation"],
    },
    {
        "id": "send",
        "title": "Send the quotation to the customer",
        "requires": {"tool_called": "post_message"},
        "gates": ["tier:outward"],
    },
]


def _spec_steps() -> tuple[Step, ...]:
    return parse_definition(
        {
            "oc8_skill": 2,
            "id": "sk-quote",
            "version": "1.0.0",
            "instruction": "Create and send a quotation.",
            "steps": SPEC_STEPS_RAW,
        }
    ).steps


def test_read_of_true_only_after_entity_last_read() -> None:
    steps = _spec_steps()
    ledger = Ledger(
        entities={
            "office/partner/1": EntityRef(
                connection="office",
                kind="partner",
                id="1",
                last_read_step=None,
            )
        }
    )
    assert "identify" not in satisfied_ids(steps, ledger, None)

    ledger.entities["office/partner/1"].last_read_step = 3
    assert "identify" in satisfied_ids(steps, ledger, None)


def test_tool_called_true_only_when_name_in_tools_called() -> None:
    steps = _spec_steps()
    ledger = Ledger()
    assert "quote" not in satisfied_ids(steps, ledger, None)

    ledger.tools_called = ["create_quotation"]
    assert "quote" in satisfied_ids(steps, ledger, None)


def test_confirmation_requires_answered_true() -> None:
    steps = _spec_steps()
    ledger = Ledger(
        decisions=[DecisionRef(tool="ask_user", question="Discount?", step=1, answered=False)]
    )
    assert "confirm_scope" not in satisfied_ids(steps, ledger, None)

    ledger.decisions[0].answered = True
    assert "confirm_scope" in satisfied_ids(steps, ledger, None)


def test_manual_true_only_when_evidence_has_step_id() -> None:
    step = Step(
        id="note",
        title="Note the reason",
        requires_kind="manual",
        requires_value="",
        required=True,
        gates=(),
    )
    steps = (step,)
    ledger = Ledger()
    assert "note" not in satisfied_ids(steps, ledger, None)
    assert "note" not in satisfied_ids(steps, ledger, ProcedureMark(evidence={}))
    assert "note" in satisfied_ids(
        steps, ledger, ProcedureMark(evidence={"note": "customer asked"})
    )


def test_checklist_matches_ruling_nine_with_first_done() -> None:
    steps = _spec_steps()
    text = checklist(steps, frozenset({"identify"}))
    assert text == (
        "Procedure checklist (the system tracks these; a gated action is refused until\n"
        "the steps before it are done):\n"
        "1. [done] Identify the customer record\n"
        "2. [ ] Confirm the discount with the requester\n"
        "3. [ ] Create the quotation  (gates: create_quotation)\n"
        "4. [ ] Send the quotation to the customer  (gates: outward actions)"
    )


def test_missing_text_for_each_kind() -> None:
    steps = _spec_steps()
    assert missing_text(steps[0]) == "no partner record has been read in this run"
    assert missing_text(steps[2]) == "tool create_quotation has not been called in this run"
    assert missing_text(steps[1]) == "no confirmation has been answered in this run"
    manual = Step(
        id="note",
        title="Note",
        requires_kind="manual",
        requires_value="",
        required=True,
        gates=(),
    )
    assert missing_text(manual) == "this step has not been marked done"
