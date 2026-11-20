from __future__ import annotations

from typing import Any

import pytest

from oc8.skills.schema import parse_definition

BASE: dict[str, Any] = {
    "oc8_skill": 2,
    "id": "sk-quote",
    "version": "1.0.0",
    "instruction": "Create and send a quotation.",
}

SPEC_STEPS: list[dict[str, Any]] = [
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


def test_parses_the_four_spec_example_steps() -> None:
    d = parse_definition({**BASE, "steps": SPEC_STEPS})
    assert len(d.steps) == 4
    assert d.steps[0].required is True
    assert d.steps[0].requires_kind == "read_of"
    assert d.steps[1].requires_kind == "confirmation"
    assert d.steps[2].requires_kind == "tool_called"
    assert d.steps[3].requires_kind == "tool_called"
    assert d.steps[2].gates == ("create_quotation",)
    assert d.steps[3].gates == ("tier:outward",)


def test_missing_steps_defaults_to_empty() -> None:
    d = parse_definition(BASE)
    assert d.steps == ()


def test_step_with_two_requires_keys_is_dropped(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oc8.skills.schema as schema_module

    warning_calls: list[tuple[Any, ...]] = []
    original_warning = schema_module.logger.warning

    def track_warning(msg: Any, *args: Any) -> None:
        warning_calls.append((msg, args))
        original_warning(msg, *args)

    monkeypatch.setattr(schema_module.logger, "warning", track_warning)

    d = parse_definition(
        {
            **BASE,
            "steps": [
                {
                    "id": "bad",
                    "title": "Bad step",
                    "requires": {"read_of": "partner", "tool_called": "create_quotation"},
                },
                SPEC_STEPS[0],
            ],
        }
    )

    assert len(d.steps) == 1
    assert d.steps[0].id == "identify"
    assert len(warning_calls) >= 1
    assert any("dropping malformed skill step" in str(call[0]) for call in warning_calls)


def test_required_defaults_to_true_when_omitted() -> None:
    step = {
        "id": "confirm_scope",
        "title": "Confirm the discount with the requester",
        "requires": {"confirmation": True},
    }
    d = parse_definition({**BASE, "steps": [step]})
    assert d.steps[0].required is True


def test_required_false_when_explicit() -> None:
    step = {
        "id": "confirm_scope",
        "title": "Confirm the discount with the requester",
        "requires": {"confirmation": True},
        "required": False,
    }
    d = parse_definition({**BASE, "steps": [step]})
    assert d.steps[0].required is False
