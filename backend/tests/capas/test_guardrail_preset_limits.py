"""A guardrail preset (and a library entry) may carry usage limits:

    limits = [{ tools = ["mail_send", "mail_reply"], max = 50, per = "day" }]

Parsed into the same shape the stored policy uses (`authz.pdp.UsageLimit`),
checked against the connection's own tools, and handed to the browser so
applying the preset writes them into the stored guardrail.
"""

from __future__ import annotations

from typing import Any

import pytest

from oc8.capas.guardrails import parse_guardrail_entry
from oc8.capas.manifest import ManifestError, ToolPackConnection, parse_guardrail_preset

SENDING = {"tools": ["mail_send", "mail_reply"], "max": 50, "per": "day"}


def _preset(**extra: Any) -> dict[str, Any]:
    return {
        "key": "autonomous_with_limit",
        "label": "Autonomous, max 50 mails a day",
        "summary": "s",
        "read": True,
        "modify": True,
        "only": ["mail_get", "mail_send", "mail_reply"],
        **extra,
    }


def _connection(preset: dict[str, Any]) -> ToolPackConnection:
    return ToolPackConnection.model_validate(
        {
            "key": "primary",
            "name": "email",
            "transport": "stdio",
            "server_url": "stdio://email",
            "scopes": {"read": ["mail_get"], "modify": ["mail_send", "mail_reply"]},
            "guardrail_presets": [preset],
        }
    )


def test_a_preset_parses_its_limits() -> None:
    preset = parse_guardrail_preset(_preset(limits=[SENDING]))
    assert [lim.to_json() for lim in preset.limits] == [SENDING]


def test_a_preset_without_limits_has_none() -> None:
    assert parse_guardrail_preset(_preset()).limits == []


@pytest.mark.parametrize(
    "bad",
    [
        {"tools": [], "max": 5},
        {"tools": ["mail_send"], "max": -1},
        {"tools": ["mail_send"], "max": 5, "per": "week"},
        {"tools": ["mail_send"], "max": 5, "extra": 1},
    ],
)
def test_a_malformed_limit_is_a_manifest_error(bad: dict[str, Any]) -> None:
    with pytest.raises(ManifestError):
        parse_guardrail_preset(_preset(limits=[bad]))


def test_a_limit_must_count_tools_the_preset_reaches() -> None:
    """Counting a tool `only` withholds would promise a budget for something
    the agent can never call -- a typo, most likely."""
    with pytest.raises(ManifestError):
        parse_guardrail_preset(_preset(limits=[{"tools": ["mail_forward"], "max": 5}]))


def test_a_limit_must_name_the_connections_own_tools() -> None:
    preset = _preset(only=[], limits=[{"tools": ["mail_sned"], "max": 5}])
    with pytest.raises(ValueError, match="unknown tool"):
        _connection(preset)
    assert _connection(_preset(limits=[SENDING])).guardrail_presets[0].limits


def test_a_library_entry_parses_its_limits() -> None:
    entry = parse_guardrail_entry(
        {
            "key": "sales_with_limit",
            "label": "l",
            "summary": "s",
            "use_case": "sales",
            "read": True,
            "modify": True,
            "limits": [{"tools": ["create_record"], "max": 20}],
        }
    )
    assert [lim.to_json() for lim in entry.limits] == [
        {"tools": ["create_record"], "max": 20, "per": "day"}
    ]


def test_a_malformed_limit_in_a_department_template_fails_the_manifest() -> None:
    """Template frames and narrowings are untyped dicts: a bad limit there
    must fail at install with a clear message, not break every run later."""
    from oc8.capas.manifest import DepartmentTemplateSpec, TemplateAgent

    bad = {"tools": {"email": {"enabled": True, "limits": [{"tools": [], "max": 5}]}}}
    with pytest.raises(ValueError, match=r"email.*usage limit"):
        DepartmentTemplateSpec.model_validate({"frame": bad})
    with pytest.raises(ValueError, match=r"email.*usage limit"):
        TemplateAgent.model_validate({"name": "Mila", "narrowing": bad})
    ok = {"tools": {"email": {"enabled": True, "limits": [SENDING]}}}
    assert DepartmentTemplateSpec.model_validate({"frame": ok}).frame == ok
