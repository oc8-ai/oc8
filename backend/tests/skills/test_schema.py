from __future__ import annotations

from typing import Any

import pytest

from oc8.skills.schema import SkillDefinitionError, parse_definition

SEEDED: dict[str, Any] = {
    "oc8_skill": 1,
    "id": "sk-invoice-check",
    "version": "1.4.2",
    "instruction": "Extract header and line items, match against open POs.",
    "requires": {"tools": ["odoo", "email"], "kbs": ["Chart of Accounts"]},
    "guardrails": ["Amounts above 10000 EUR require human approval"],
}


def test_parses_the_seeded_shape() -> None:
    d = parse_definition(SEEDED)
    assert d.schema_version == 1
    assert d.slug == "sk-invoice-check"
    assert d.version == "1.4.2"
    assert d.instruction.startswith("Extract header")
    assert [r.tool for r in d.requires_tools] == ["odoo", "email"]
    assert d.requires_kbs == ("Chart of Accounts",)


def test_bare_string_requirement_defaults_to_read() -> None:
    # Mirrors missing_skill_requirements' own default so assignment-time and
    # runtime agree on what a bare string means.
    d = parse_definition(SEEDED)
    assert d.requires_tools[0].rights == ("read",)


def test_structured_requirement_keeps_its_rights() -> None:
    d = parse_definition(
        {**SEEDED, "requires": {"tools": [{"tool": "odoo", "rights": ["read", "write"]}]}}
    )
    assert d.requires_tools[0].tool == "odoo"
    assert d.requires_tools[0].rights == ("read", "write")


def test_structured_guardrail_is_parsed() -> None:
    d = parse_definition(
        {
            **SEEDED,
            "guardrails": [
                {
                    "type": "value_threshold",
                    "action": "send_action",
                    "metric": "value_eur",
                    "gt": 5000,
                    "then": "require_approval",
                }
            ],
        }
    )
    assert len(d.guardrails) == 1
    g = d.guardrails[0]
    assert g.type == "value_threshold"
    assert g.gt == 5000.0
    assert g.then == "require_approval"
    assert d.prose_guardrails == ()


def test_prose_guardrails_are_kept_separately() -> None:
    d = parse_definition(SEEDED)
    assert d.guardrails == ()
    assert d.prose_guardrails == ("Amounts above 10000 EUR require human approval",)


def test_a_malformed_guardrail_is_dropped_not_raised() -> None:
    # A single bad guardrail must not cost the whole skill: the run would fail
    # for a reason unrelated to what the agent was asked to do.
    d = parse_definition(
        {
            **SEEDED,
            "guardrails": [
                {"type": "value_threshold"},  # missing gt/then
                {
                    "type": "value_threshold",
                    "action": "send_action",
                    "metric": "value_eur",
                    "gt": 100,
                    "then": "require_approval",
                },
            ],
        }
    )
    assert len(d.guardrails) == 1
    assert d.guardrails[0].gt == 100.0


def test_missing_instruction_raises() -> None:
    bad = {k: v for k, v in SEEDED.items() if k != "instruction"}
    with pytest.raises(SkillDefinitionError, match="instruction"):
        parse_definition(bad)


def test_empty_instruction_raises() -> None:
    with pytest.raises(SkillDefinitionError, match="instruction"):
        parse_definition({**SEEDED, "instruction": "   "})


def test_missing_requires_is_tolerated() -> None:
    bare = {"oc8_skill": 1, "id": "x", "version": "1.0.0", "instruction": "do it"}
    d = parse_definition(bare)
    assert d.requires_tools == ()
    assert d.requires_kbs == ()
    assert d.guardrails == ()


def test_non_mapping_raises() -> None:
    with pytest.raises(SkillDefinitionError):
        parse_definition([])  # type: ignore[arg-type]


def test_non_numeric_schema_version_falls_back_to_1() -> None:
    # A malformed oc8_skill value must degrade, not raise a bare ValueError --
    # this parser runs per-run against rows it does not control.
    d = parse_definition({**SEEDED, "oc8_skill": "not-a-number"})
    assert d.schema_version == 1


def test_non_str_non_mapping_guardrail_is_dropped_not_raised(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oc8.skills.schema as schema_module

    # Track warning calls since caplog doesn't capture logs from this logger
    warning_calls: list[tuple[Any, ...]] = []
    original_warning = schema_module.logger.warning

    def track_warning(msg: Any, *args: Any) -> None:
        warning_calls.append((msg, args))
        original_warning(msg, *args)

    monkeypatch.setattr(schema_module.logger, "warning", track_warning)

    d = parse_definition({**SEEDED, "guardrails": [42, None]})

    assert d.guardrails == ()
    assert d.prose_guardrails == ()
    # Verify that warnings were emitted for non-str/non-Mapping guardrails
    assert len(warning_calls) >= 1
    assert any("dropping malformed skill guardrail" in str(call[0]) for call in warning_calls)


def test_missing_rights_key_defaults_to_read() -> None:
    d = parse_definition(
        {**SEEDED, "requires": {"tools": [{"tool": "odoo"}]}}
    )
    assert d.requires_tools[0].rights == ("read",)


def test_explicit_empty_rights_stays_empty() -> None:
    # Mirrors authz.pdp.missing_skill_requirements: req.get("rights", ["read"])
    # defaults only when the key is absent, not when it's an explicit [].
    d = parse_definition(
        {**SEEDED, "requires": {"tools": [{"tool": "odoo", "rights": []}]}}
    )
    assert d.requires_tools[0].rights == ()


def test_reference_root_round_trips() -> None:
    d = parse_definition({**SEEDED, "reference_root": "my-capa/skills/thai-compliance"})
    assert d.reference_root == "my-capa/skills/thai-compliance"


def test_absent_reference_root_is_none() -> None:
    d = parse_definition(SEEDED)
    assert d.reference_root is None


def test_empty_string_reference_root_is_treated_as_none() -> None:
    d = parse_definition({**SEEDED, "reference_root": ""})
    assert d.reference_root is None


def test_code_mode_is_on_only_when_the_skill_says_true() -> None:
    assert parse_definition({**SEEDED, "code_mode": True}).code_mode is True
    assert parse_definition(SEEDED).code_mode is False
    assert parse_definition({**SEEDED, "code_mode": "yes"}).code_mode is False


def test_non_string_reference_root_is_dropped_not_raised() -> None:
    # Tolerant on the way in, same as every other field here -- a malformed
    # value must not cost a run that is otherwise fine.
    d = parse_definition({**SEEDED, "reference_root": 42})
    assert d.reference_root is None
