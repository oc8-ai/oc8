"""A1 (system prompt v2) and timezone-resolution behavior. Package 3 --
office agent harness spec §4."""

from __future__ import annotations

import uuid
import zoneinfo

from oc8 import models as m
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.harness.prompts import render_system_prompt, resolve_timezone


def _agent(**overrides) -> m.Agent:
    defaults: dict = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Nora",
        role_title="Sales assistant",
        mission="Keep the pipeline moving.",
        presentation={"guardrails": ["Never discount more than 10% without approval."]},
        is_tenant_assistant=False,
    )
    defaults.update(overrides)
    return m.Agent(**defaults)


def test_sequential_parallel_rule_when_caps_disallow_it():
    prompt = render_system_prompt(
        _agent(), caps=ModelCaps(parallel_tool_calls=False), tenant_name="Acme"
    )
    assert "Call one tool at a time and wait for its result." in prompt
    assert "You may issue several read-only calls in one turn" not in prompt


def test_parallel_rule_when_caps_allow_it():
    prompt = render_system_prompt(
        _agent(), caps=ModelCaps(parallel_tool_calls=True), tenant_name="Acme"
    )
    assert "You may issue several read-only calls in one turn" in prompt
    assert "Call one tool at a time and wait for its result." not in prompt


def test_tenant_name_is_interpolated():
    prompt = render_system_prompt(_agent(), caps=ModelCaps(), tenant_name="Acme Corp")
    assert "on behalf of Acme Corp" in prompt


def test_guardrails_section_omitted_when_empty():
    prompt = render_system_prompt(_agent(presentation={}), caps=ModelCaps(), tenant_name="Acme")
    assert "Guardrails you must respect" not in prompt


def test_guardrails_section_present_when_declared():
    prompt = render_system_prompt(_agent(), caps=ModelCaps(), tenant_name="Acme")
    assert (
        "Guardrails you must respect:\n- Never discount more than 10% without approval." in prompt
    )


def test_role_title_omitted_when_blank():
    prompt = render_system_prompt(_agent(role_title=""), caps=ModelCaps(), tenant_name="Acme")
    assert prompt.startswith("You are Nora.")


def test_role_title_included_when_present():
    prompt = render_system_prompt(_agent(), caps=ModelCaps(), tenant_name="Acme")
    assert prompt.startswith("You are Nora, Sales assistant.")


def test_prompt_is_byte_stable_across_calls():
    """A1: no dynamic values -- calling twice with the same inputs must be identical."""
    a = render_system_prompt(_agent(), caps=ModelCaps(), tenant_name="Acme")
    b = render_system_prompt(_agent(), caps=ModelCaps(), tenant_name="Acme")
    assert a == b


def test_resolve_timezone_valid():
    label, tz = resolve_timezone("Europe/Berlin")
    assert label == "Europe/Berlin"
    assert isinstance(tz, zoneinfo.ZoneInfo)


def test_resolve_timezone_falls_back_to_utc_on_garbage():
    label, tz = resolve_timezone("Not/A_Real_Zone")
    assert label == "UTC"
    assert tz == zoneinfo.ZoneInfo("UTC")


def test_resolve_timezone_falls_back_to_utc_on_empty():
    label, tz = resolve_timezone("")
    assert label == "UTC"
    assert tz == zoneinfo.ZoneInfo("UTC")
