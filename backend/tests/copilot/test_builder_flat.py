"""propose_agent's flat shape (small models) expands into exactly the
operations the explicit form carries, and is judged by the same parser."""

from __future__ import annotations

import uuid
from typing import Any

import jsonschema
import pytest

from oc8.agent.control_tools import PROPOSE_AGENT
from oc8.copilot.builder_ops import (
    BuilderInvalid,
    builder_operation_data,
    flat_operations,
    parse_builder_operations,
    proposal_operations,
)

DEPT = str(uuid.uuid4())
MODEL = str(uuid.uuid4())
SKILL = str(uuid.uuid4())
FRAME = {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}}
MISSION = "Prüft jeden Morgen offene Rechnungen."


def _flat(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "departmentId": DEPT,
        "name": "Rechnungsprüfer",
        "roleTitle": "Buchhaltung",
        "mission": MISSION,
        "modelId": MODEL,
        "skillIds": [SKILL],
        "schedule": "0 7 * * 1-5",
        "systems": ["odoo"],
        "limits": [
            {"system": "odoo", "function": "create_invoice", "decision": "approval_required"}
        ],
    }
    fields.update(overrides)
    return fields


def _explicit() -> list[dict[str, Any]]:
    return [
        {
            "type": "agent.create",
            "ref": "$new",
            "departmentId": DEPT,
            "name": "Rechnungsprüfer",
            "roleTitle": "Buchhaltung",
            "mission": MISSION,
        },
        {"type": "agent.model.switch", "agentRef": "$new", "modelConfigId": MODEL},
        {"type": "agent.skill.assign", "agentRef": "$new", "skillVersionId": SKILL},
        {
            "type": "trigger.create",
            "agentRef": "$new",
            "kind": "cron",
            "cronExpression": "0 7 * * 1-5",
            "taskText": MISSION,
        },
        {
            "type": "agent.narrowing.set",
            "agentRef": "$new",
            "narrowing": {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}},
        },
        {
            "type": "agent.guardrail.set",
            "agentRef": "$new",
            "connectionName": "odoo",
            "function": "create_invoice",
            "decision": "approval_required",
        },
    ]


def _stored(raw: object) -> list[dict[str, Any]]:
    return [builder_operation_data(s) for s in parse_builder_operations(raw)]


def _code(raw: object) -> tuple[str, int | None]:
    with pytest.raises(BuilderInvalid) as caught:
        parse_builder_operations(raw)
    return caught.value.code, caught.value.step


def test_the_schema_is_valid_json_schema_and_accepts_both_shapes() -> None:
    schema = PROPOSE_AGENT.parameters
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(_flat())
    validator.validate({"operations": _explicit()})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"operations": [{"type": "department.create", "name": "X"}]})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"operations": [{**_explicit()[1], "agentRef": "$other"}]})


def test_the_schema_stays_within_what_every_provider_takes() -> None:
    """No oneOf/anyOf/allOf anywhere -- no other tool schema here uses them, and
    the Ollama path forwards nested schemas only as an opaque blob."""

    def keys(node: object) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {k for v in node.values() for k in keys(v)}
        if isinstance(node, list):
            return {k for v in node for k in keys(v)}
        return set()

    assert not keys(PROPOSE_AGENT.parameters) & {"oneOf", "anyOf", "allOf", "$ref"}
    assert PROPOSE_AGENT.parameters["type"] == "object"


def test_the_flat_form_expands_to_the_explicit_operations() -> None:
    assert flat_operations(_flat(), frame=FRAME) == _explicit()
    assert _stored(proposal_operations(_flat(), frame=FRAME)) == _stored(_explicit())


def test_a_flat_event_trigger_and_an_own_task_text() -> None:
    ops = flat_operations(
        _flat(schedule=None, eventSource="odoo", eventType="invoice.created", taskText="Prüfe sie.")
    )
    (trigger,) = [op for op in ops if op["type"] == "trigger.create"]
    assert trigger == {
        "type": "trigger.create",
        "agentRef": "$new",
        "kind": "event",
        "eventSource": "odoo",
        "eventType": "invoice.created",
        "taskText": "Prüfe sie.",
    }
    _stored(ops)


def test_left_out_optional_fields_add_no_steps() -> None:
    ops = flat_operations(
        {"departmentId": DEPT, "name": "X", "mission": "Y", "modelId": None, "skillIds": []}
    )
    assert [op["type"] for op in ops] == ["agent.create"]
    assert flat_operations(_flat(skillIds=SKILL))[2]["skillVersionId"] == SKILL, "one id"


def test_a_system_never_gets_more_than_the_frame_grants() -> None:
    ops = flat_operations(_flat(limits=None), frame=None)
    assert ops[-1]["narrowing"] == {
        "tools": {"odoo": {"enabled": True, "read": False, "modify": False}}
    }


def test_a_system_the_frame_does_not_list_may_read_but_not_write() -> None:
    """An agent-exclusive grant (pdp.narrowing_within_frame): reading on,
    writes stay off until a person grants them."""
    ops = flat_operations(_flat(limits=None), frame={"tools": {"microsoft365": {"enabled": True}}})
    assert ops[-1]["narrowing"] == {
        "tools": {"odoo": {"enabled": True, "read": True, "modify": False}}
    }


@pytest.mark.parametrize(
    ("flat", "explicit_step"),
    [
        ({"name": None}, {"name": None}),
        ({"departmentId": "not-an-id"}, {"departmentId": "not-an-id"}),
        ({"name": "x" * 201}, {"name": "x" * 201}),
    ],
)
def test_a_bad_create_field_is_refused_like_the_explicit_form(
    flat: dict[str, Any], explicit_step: dict[str, Any]
) -> None:
    explicit = _explicit()
    explicit[0] = {k: v for k, v in {**explicit[0], **explicit_step}.items() if v is not None}
    expected = _code(explicit)
    assert expected == ("invalid", 0)
    assert _code(proposal_operations(_flat(**flat), frame=FRAME)) == expected


@pytest.mark.parametrize(
    ("flat", "step"),
    [
        ({"modelId": "not-an-id"}, 1),
        ({"skillIds": ["not-an-id"]}, 2),
        ({"schedule": "x" * 300}, 3),
        ({"limits": [{"system": "odoo", "function": "f", "decision": "maybe"}]}, 5),
        ({"limits": [{"system": "odoo", "function": "f", "decision": "not_allowed", "x": 1}]}, 5),
    ],
)
def test_a_bad_setting_is_refused_at_its_step(flat: dict[str, Any], step: int) -> None:
    assert _code(proposal_operations(_flat(**flat), frame=FRAME)) == ("invalid", step)


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({**_flat(), "operations": _explicit()}, "mixed_shapes"),
        ({}, "missing_agent"),
        ({"departmentId": None, "name": ""}, "missing_agent"),
        (_flat(model=MODEL), "unknown_field"),
        (_flat(skillIds=7), "invalid"),
        (_flat(limits=["odoo"]), "invalid"),
        ("not an object", "invalid"),
    ],
)
def test_a_malformed_call_is_refused_before_parsing(arguments: object, code: str) -> None:
    with pytest.raises(BuilderInvalid) as caught:
        proposal_operations(arguments, frame=FRAME)
    assert caught.value.code == code
    assert "Rechnungsprüfer" not in str(caught.value), "value-free"


def test_the_explicit_form_passes_through_untouched() -> None:
    ops = _explicit()
    assert proposal_operations({"operations": ops}) is ops
    assert proposal_operations({"operations": ops, "name": None}) is ops
