"""Procedure satisfaction, B3 denial, and C5 flip lines (Package 9)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from oc8.agent.harness import GateVerdict, Harness
from oc8.agent.harness.procedures import (
    checklist,
    missing_text,
    newly_satisfied_lines,
    procedure_denial,
    satisfied_ids,
)
from oc8.agent.harness.state import DecisionRef, EntityRef, Ledger, ProcedureMark
from oc8.modelrouter import ToolCall
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


def _quote_skill(
    steps: tuple[Step, ...] | None = None,
) -> tuple[str, str, tuple[Step, ...]]:
    return ("quote", "Quote", steps if steps is not None else _spec_steps())


def test_procedure_denial_blocks_create_quotation_until_identify() -> None:
    steps = _spec_steps()
    ledger = Ledger()
    hit = procedure_denial(
        tool="create_quotation",
        tier="write",
        skills=[_quote_skill(steps)],
        ledger=ledger,
        procedure={},
    )
    assert hit is not None
    reason, slug = hit
    assert slug == "quote"
    assert 'Procedure "Quote", step 1 "Identify the customer record"' in reason
    assert "no partner record has been read in this run" in reason
    assert "policy decision" in reason


def test_procedure_denial_allows_create_quotation_after_identify() -> None:
    # confirm_scope required=False so only identify blocks the quote gate.
    base = _spec_steps()
    steps = (base[0], replace(base[1], required=False), base[2], base[3])
    ledger = Ledger(
        entities={
            "office/partner/1": EntityRef(
                connection="office",
                kind="partner",
                id="1",
                last_read_step=2,
            )
        }
    )
    assert (
        procedure_denial(
            tool="create_quotation",
            tier="write",
            skills=[_quote_skill(steps)],
            ledger=ledger,
            procedure={},
        )
        is None
    )


def test_procedure_denial_tier_outward_gates_unrelated_tool_name() -> None:
    steps = _spec_steps()
    hit = procedure_denial(
        tool="post_message",
        tier="outward",
        skills=[_quote_skill(steps)],
        ledger=Ledger(),
        procedure={},
    )
    assert hit is not None
    reason, _ = hit
    assert 'Procedure "Quote", step 1 "Identify the customer record"' in reason


def test_procedure_denial_none_when_tool_not_gated() -> None:
    assert (
        procedure_denial(
            tool="search_records",
            tier="read",
            skills=[_quote_skill()],
            ledger=Ledger(),
            procedure={},
        )
        is None
    )


def test_newly_satisfied_lines_flip_format() -> None:
    steps = _spec_steps()
    lines = newly_satisfied_lines(
        skills=[_quote_skill(steps)],
        before={"quote": frozenset()},
        after={"quote": frozenset({"identify"})},
    )
    assert lines == [
        "Procedure 'Quote': step 1 'Identify the customer record' satisfied; "
        "next: 2 'Confirm the discount with the requester'"
    ]


def test_gate_procedure_denial_before_read_before_write() -> None:
    h = Harness()
    steps = _spec_steps()
    verdict = h.gate(
        ToolCall(id="c", name="create_quotation", arguments={}),
        tier="write",
        ledger=h.state.ledger,
        connection="office",
        config={"read_before_write": False},
        autonomy="default",
        granted=False,
        record_label="",
        identity=None,
        procedures=[_quote_skill(steps)],
    )
    assert verdict.effect == "deny"
    assert verdict.rule == "procedure"
    assert verdict == GateVerdict(
        effect="deny",
        reason=verdict.reason,
        rule="procedure",
        tier="write",
    )
    assert "do not retry the same change another way" not in verdict.reason
    assert 'Procedure "Quote", step 1' in verdict.reason


def test_may_finish_procedure_continuation_then_exhausted() -> None:
    from oc8.agent.harness import Harness
    from oc8.skills.schema import Step

    steps = (
        Step(
            id="identify",
            title="Identify the customer record",
            requires_kind="read_of",
            requires_value="partner",
            required=True,
            gates=(),
        ),
    )
    h = Harness()
    procs = [("quote", "Quote", steps)]
    v1 = h.may_finish([], can_continue=True, procedures=procs)
    assert v1.ok is False
    assert v1.reminder is not None
    assert "continuation round 1/3" in v1.reminder
    assert h.state.procedure_rounds == 1
    h.may_finish([], can_continue=True, procedures=procs)
    h.may_finish([], can_continue=True, procedures=procs)
    v4 = h.may_finish([], can_continue=True, procedures=procs)
    assert v4.ok is True
    assert v4.exhausted_note is not None
    assert "required procedure steps still open" in v4.exhausted_note
    assert h.state.procedure_rounds == 3


def test_procedure_haystack_omits_tier_gates() -> None:
    from oc8.agent.harness.procedures import procedure_haystack
    from oc8.skills.schema import Step

    steps = (
        Step(
            id="quote",
            title="Create the quotation",
            requires_kind="tool_called",
            requires_value="create_quotation",
            required=True,
            gates=("create_quotation", "tier:outward"),
        ),
    )
    text = procedure_haystack(steps)
    assert "Create the quotation" in text
    assert "create_quotation" in text
    assert "tier:outward" not in text
    assert "outward" not in text or "create_quotation" in text.split("\n")
